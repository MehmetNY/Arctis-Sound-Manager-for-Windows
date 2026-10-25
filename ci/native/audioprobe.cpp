// audioprobe.cpp - small Windows audio experiments used by the ASM Windows port's CI.
//
//   audioprobe list
//   audioprobe tone     --device <substr> [--secs 10] [--amp 0.5] [--freq 440]
//   audioprobe record   --device <substr> [--secs 3] [--loopback]          (per-channel RMS of an endpoint)
//   audioprobe capture  --pid <pid> [--exclude] [--secs 3] [--dump f.f32]   (WASAPI process loopback, float32 stereo 48 kHz)
//   audioprobe sessions
//   audioprobe setvol   --pid <pid> --vol <0..1>
//   audioprobe mute     --pid <pid> --on|--off
//   audioprobe epvol    --device <substr> --vol <0..1>
//   audioprobe epmute   --device <substr> --on|--off
//
// Every measurement prints one line starting with "RESULT".
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <initguid.h>
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <endpointvolume.h>
#include <functiondiscoverykeys_devpkey.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

// ---- the bits of audioclientactivationparams.h we need (layout is what matters) ----
DEFINE_GUID(ASM_IID_IAgileObject, 0x94ea2b94, 0xe9cc, 0x49e0, 0xc0, 0xff, 0xee, 0x64, 0xca, 0x8f, 0x5b, 0x90);
DEFINE_GUID(ASM_IID_ActivateHandler, 0x41d949ab, 0x9862, 0x444a, 0x80, 0xf6, 0xc2, 0x61, 0x33, 0x4d, 0xa5, 0xeb);
DEFINE_GUID(ASM_SUBTYPE_FLOAT, 0x00000003, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);
DEFINE_GUID(ASM_SUBTYPE_PCM, 0x00000001, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);

struct AsmLoopbackParams { DWORD TargetProcessId; int Mode; };          // 0 = include tree, 1 = exclude tree
struct AsmActivationParams { int ActivationType; AsmLoopbackParams Loopback; };   // 1 = process loopback

template <class T> struct Ptr {
    T* p = nullptr;
    Ptr() {}
    ~Ptr() { reset(); }
    Ptr(const Ptr&) = delete;
    Ptr& operator=(const Ptr&) = delete;
    void reset() { if (p) { p->Release(); p = nullptr; } }
    T** operator&() { reset(); return &p; }
    T* operator->() const { return p; }
    explicit operator bool() const { return p != nullptr; }
};

static std::wstring W(const char* s) { int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, nullptr, 0); std::wstring w(n, 0); MultiByteToWideChar(CP_UTF8, 0, s, -1, &w[0], n); w.resize(n - 1); return w; }
static std::string U(const std::wstring& w) { if (w.empty()) return ""; int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, nullptr, 0, nullptr, nullptr); std::string s(n, 0); WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, &s[0], n, nullptr, nullptr); s.resize(n - 1); return s; }
static std::wstring lower(std::wstring s) { for (auto& c : s) c = (wchar_t)towlower(c); return s; }

static const char* arg(int argc, char** argv, const char* name, const char* def = nullptr) {
    for (int i = 2; i < argc - 1; i++) if (!strcmp(argv[i], name)) return argv[i + 1];
    return def;
}
static bool flag(int argc, char** argv, const char* name) {
    for (int i = 2; i < argc; i++) if (!strcmp(argv[i], name)) return true;
    return false;
}

static std::string hr(HRESULT h) { char b[32]; snprintf(b, sizeof b, "0x%08lX", (unsigned long)h); return b; }

static Ptr<IMMDeviceEnumerator> g_enum;
static bool initEnum() {
    if (g_enum) return true;
    HRESULT h = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&g_enum);
    if (FAILED(h)) { printf("ERROR enumerator %s\n", hr(h).c_str()); return false; }
    return true;
}

static std::wstring friendly(IMMDevice* dev) {
    Ptr<IPropertyStore> ps;
    if (FAILED(dev->OpenPropertyStore(STGM_READ, &ps))) return L"?";
    PROPVARIANT v; PropVariantInit(&v);
    std::wstring r = L"?";
    if (SUCCEEDED(ps->GetValue(PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR && v.pwszVal) r = v.pwszVal;
    PropVariantClear(&v);
    return r;
}

// An active endpoint whose friendly name contains `needle` (case-insensitive). Prefers the shortest name
// so that "CABLE Input" does not match "CABLE In 16 Ch" and vice versa.
static bool findDevice(EDataFlow flow, const std::wstring& needle, Ptr<IMMDevice>& out, std::wstring* name = nullptr) {
    if (!initEnum()) return false;
    Ptr<IMMDeviceCollection> col;
    if (FAILED(g_enum->EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE, &col))) return false;
    UINT n = 0; col->GetCount(&n);
    size_t bestLen = (size_t)-1;
    for (UINT i = 0; i < n; i++) {
        Ptr<IMMDevice> d;
        if (FAILED(col->Item(i, &d))) continue;
        std::wstring f = friendly(d.p);
        if (lower(f).find(lower(needle)) == std::wstring::npos) continue;
        if (f.size() < bestLen) { bestLen = f.size(); out.reset(); out.p = d.p; d.p = nullptr; if (name) *name = f; }
    }
    return (bool)out;
}

struct Fmt { int ch = 2; int bits = 32; bool isFloat = true; int align = 8; int rate = 48000; };
static Fmt fmtOf(const WAVEFORMATEX* w) {
    Fmt f; f.ch = w->nChannels; f.bits = w->wBitsPerSample; f.align = w->nBlockAlign; f.rate = (int)w->nSamplesPerSec;
    f.isFloat = (w->wFormatTag == WAVE_FORMAT_IEEE_FLOAT);
    if (w->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        auto* e = (const WAVEFORMATEXTENSIBLE*)w;
        f.isFloat = IsEqualGUID(e->SubFormat, ASM_SUBTYPE_FLOAT) != 0;
    }
    return f;
}
static float sampleAt(const BYTE* p, const Fmt& f) {
    if (f.isFloat && f.bits == 32) return *(const float*)p;
    if (f.bits == 16) return *(const int16_t*)p / 32768.0f;
    if (f.bits == 24) { int v = (p[0] << 8) | (p[1] << 16) | (p[2] << 24); return (v >> 8) / 8388608.0f; }
    if (f.bits == 32) return *(const int32_t*)p / 2147483648.0f;
    return 0.0f;
}
static void putSample(BYTE* p, const Fmt& f, float v) {
    if (v > 1.f) v = 1.f;
    if (v < -1.f) v = -1.f;
    if (f.isFloat && f.bits == 32) *(float*)p = v;
    else if (f.bits == 16) *(int16_t*)p = (int16_t)lrintf(v * 32767.f);
    else if (f.bits == 24) { int x = (int)lrintf(v * 8388607.f); p[0] = x & 0xff; p[1] = (x >> 8) & 0xff; p[2] = (x >> 16) & 0xff; }
    else if (f.bits == 32) *(int32_t*)p = (int32_t)llrintf(v * 2147483647.f);
}
static std::string fmtStr(const WAVEFORMATEX* w) {
    Fmt f = fmtOf(w); char b[96];
    snprintf(b, sizeof b, "%dch %dHz %dbit %s", f.ch, f.rate, f.bits, f.isFloat ? "float" : "pcm");
    return b;
}

// ---------------------------------------------------------------- list
static int cmdList() {
    if (!initEnum()) return 1;
    for (int flowI = 0; flowI < 2; flowI++) {
        EDataFlow flow = flowI == 0 ? eRender : eCapture;
        Ptr<IMMDeviceCollection> col;
        if (FAILED(g_enum->EnumAudioEndpoints(flow, DEVICE_STATEMASK_ALL, &col))) continue;
        UINT n = 0; col->GetCount(&n);
        for (UINT i = 0; i < n; i++) {
            Ptr<IMMDevice> d;
            if (FAILED(col->Item(i, &d))) continue;
            DWORD state = 0; d->GetState(&state);
            std::string mix = "-";
            if (state == DEVICE_STATE_ACTIVE) {
                Ptr<IAudioClient> c;
                if (SUCCEEDED(d->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&c))) {
                    WAVEFORMATEX* w = nullptr;
                    if (SUCCEEDED(c->GetMixFormat(&w)) && w) { mix = fmtStr(w); CoTaskMemFree(w); }
                }
            }
            printf("DEVICE %s state=0x%lx mix=[%s] %s\n", flow == eRender ? "render " : "capture", (unsigned long)state, mix.c_str(), U(friendly(d.p)).c_str());
        }
    }
    Ptr<IMMDevice> def;
    if (SUCCEEDED(g_enum->GetDefaultAudioEndpoint(eRender, eConsole, &def))) printf("DEFAULT render  %s\n", U(friendly(def.p)).c_str());
    def.reset();
    if (SUCCEEDED(g_enum->GetDefaultAudioEndpoint(eCapture, eConsole, &def))) printf("DEFAULT capture %s\n", U(friendly(def.p)).c_str());
    return 0;
}

// ---------------------------------------------------------------- tone
static int cmdTone(int argc, char** argv) {
    const char* dev = arg(argc, argv, "--device");
    double secs = atof(arg(argc, argv, "--secs", "10"));
    double amp = atof(arg(argc, argv, "--amp", "0.5"));
    double freq = atof(arg(argc, argv, "--freq", "440"));
    if (!dev) { printf("ERROR --device\n"); return 2; }
    Ptr<IMMDevice> d; std::wstring name;
    if (!findDevice(eRender, W(dev), d, &name)) { printf("ERROR no render device matching '%s'\n", dev); return 3; }
    Ptr<IAudioClient> c;
    HRESULT h = d->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&c);
    if (FAILED(h)) { printf("ERROR activate %s\n", hr(h).c_str()); return 4; }
    WAVEFORMATEX* w = nullptr; c->GetMixFormat(&w);
    Fmt f = fmtOf(w);
    h = c->Initialize(AUDCLNT_SHAREMODE_SHARED, 0, 2000000, 0, w, nullptr);
    if (FAILED(h)) { printf("ERROR initialize %s\n", hr(h).c_str()); return 5; }
    UINT32 bufFrames = 0; c->GetBufferSize(&bufFrames);
    Ptr<IAudioRenderClient> r;
    h = c->GetService(__uuidof(IAudioRenderClient), (void**)&r);
    if (FAILED(h)) { printf("ERROR render service %s\n", hr(h).c_str()); return 6; }
    printf("TONE start pid=%lu device='%s' mix=[%s] amp=%.3f freq=%.0f\n", (unsigned long)GetCurrentProcessId(), U(name).c_str(), fmtStr(w).c_str(), amp, freq);
    fflush(stdout);
    c->Start();
    double phase = 0, step = 2 * 3.14159265358979323846 * freq / f.rate;
    ULONGLONG end = GetTickCount64() + (ULONGLONG)(secs * 1000);
    while (GetTickCount64() < end) {
        UINT32 pad = 0; c->GetCurrentPadding(&pad);
        UINT32 n = bufFrames - pad;
        if (n) {
            BYTE* buf = nullptr;
            if (SUCCEEDED(r->GetBuffer(n, &buf))) {
                for (UINT32 i = 0; i < n; i++) {
                    float v = (float)(amp * sin(phase)); phase += step;
                    for (int k = 0; k < f.ch; k++) putSample(buf + (size_t)i * f.align + (size_t)k * (f.bits / 8), f, v);
                }
                r->ReleaseBuffer(n, 0);
            }
        }
        Sleep(5);
    }
    c->Stop();
    CoTaskMemFree(w);
    return 0;
}

// ---------------------------------------------------------------- record (endpoint, optionally loopback)
static int cmdRecord(int argc, char** argv) {
    const char* dev = arg(argc, argv, "--device");
    double secs = atof(arg(argc, argv, "--secs", "3"));
    bool loop = flag(argc, argv, "--loopback");
    if (!dev) { printf("ERROR --device\n"); return 2; }
    Ptr<IMMDevice> d; std::wstring name;
    if (!findDevice(loop ? eRender : eCapture, W(dev), d, &name)) { printf("ERROR no device matching '%s'\n", dev); return 3; }
    Ptr<IAudioClient> c;
    HRESULT h = d->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&c);
    if (FAILED(h)) { printf("ERROR activate %s\n", hr(h).c_str()); return 4; }
    WAVEFORMATEX* w = nullptr; c->GetMixFormat(&w);
    Fmt f = fmtOf(w);
    h = c->Initialize(AUDCLNT_SHAREMODE_SHARED, loop ? AUDCLNT_STREAMFLAGS_LOOPBACK : 0, 2000000, 0, w, nullptr);
    if (FAILED(h)) { printf("ERROR initialize %s\n", hr(h).c_str()); return 5; }
    Ptr<IAudioCaptureClient> cap;
    h = c->GetService(__uuidof(IAudioCaptureClient), (void**)&cap);
    if (FAILED(h)) { printf("ERROR capture service %s\n", hr(h).c_str()); return 6; }
    int nch = f.ch > 16 ? 16 : f.ch;
    std::vector<double> ss(nch, 0.0); std::vector<float> pk(nch, 0.f); ULONGLONG frames = 0;
    c->Start();
    ULONGLONG t0 = GetTickCount64(), end = t0 + (ULONGLONG)(secs * 1000), warm = t0 + 400;
    while (GetTickCount64() < end) {
        Sleep(10);
        UINT32 pkt = 0;
        while (SUCCEEDED(cap->GetNextPacketSize(&pkt)) && pkt > 0) {
            BYTE* data = nullptr; UINT32 n = 0; DWORD fl = 0;
            if (FAILED(cap->GetBuffer(&data, &n, &fl, nullptr, nullptr))) break;
            if (GetTickCount64() > warm) {
                for (UINT32 i = 0; i < n; i++)
                    for (int k = 0; k < nch; k++) {
                        float v = (fl & AUDCLNT_BUFFERFLAGS_SILENT) ? 0.f : sampleAt(data + (size_t)i * f.align + (size_t)k * (f.bits / 8), f);
                        ss[k] += (double)v * v; if (fabsf(v) > pk[k]) pk[k] = fabsf(v);
                    }
                frames += n;
            }
            cap->ReleaseBuffer(n);
        }
    }
    c->Stop();
    printf("RESULT record device='%s' loopback=%d mix=[%s] frames=%llu", U(name).c_str(), loop ? 1 : 0, fmtStr(w).c_str(), (unsigned long long)frames);
    for (int k = 0; k < nch; k++) printf(" ch%d_rms=%.7f", k + 1, frames ? sqrt(ss[k] / (double)frames) : 0.0);
    printf("\n");
    CoTaskMemFree(w);
    return 0;
}

// ---------------------------------------------------------------- process loopback
class ActivateHandler : public IActivateAudioInterfaceCompletionHandler {
    LONG ref_ = 1;
public:
    HANDLE done;
    ActivateHandler() { done = CreateEventW(nullptr, TRUE, FALSE, nullptr); }
    virtual ~ActivateHandler() { CloseHandle(done); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID r, void** o) override {
        // IAgileObject has no methods: answering for it just tells COM the object may be called from any thread.
        if (IsEqualIID(r, IID_IUnknown) || IsEqualIID(r, ASM_IID_ActivateHandler) || IsEqualIID(r, ASM_IID_IAgileObject)) { *o = static_cast<IActivateAudioInterfaceCompletionHandler*>(this); AddRef(); return S_OK; }
        *o = nullptr; return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override { LONG n = InterlockedDecrement(&ref_); if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation*) override { SetEvent(done); return S_OK; }
};

static int cmdCapture(int argc, char** argv) {
    const char* pidS = arg(argc, argv, "--pid");
    double secs = atof(arg(argc, argv, "--secs", "3"));
    bool excl = flag(argc, argv, "--exclude");
    const char* dump = arg(argc, argv, "--dump");
    if (!pidS) { printf("ERROR --pid\n"); return 2; }
    AsmActivationParams ap = {}; ap.ActivationType = 1; ap.Loopback.TargetProcessId = (DWORD)strtoul(pidS, nullptr, 10); ap.Loopback.Mode = excl ? 1 : 0;
    PROPVARIANT pv; PropVariantInit(&pv); pv.vt = VT_BLOB; pv.blob.cbSize = sizeof ap; pv.blob.pBlobData = (BYTE*)&ap;
    ActivateHandler* hd = new ActivateHandler();
    Ptr<IActivateAudioInterfaceAsyncOperation> op;
    HRESULT h = ActivateAudioInterfaceAsync(L"VAD\\Process_Loopback", __uuidof(IAudioClient), &pv, hd, &op);
    if (FAILED(h)) { printf("ERROR ActivateAudioInterfaceAsync %s\n", hr(h).c_str()); hd->Release(); return 3; }
    if (WaitForSingleObject(hd->done, 8000) != WAIT_OBJECT_0) { printf("ERROR activation timeout\n"); hd->Release(); return 4; }
    HRESULT hAct = E_FAIL; IUnknown* unk = nullptr;
    h = op->GetActivateResult(&hAct, &unk);
    hd->Release();
    if (FAILED(h) || FAILED(hAct) || !unk) { printf("ERROR activate result %s / %s\n", hr(h).c_str(), hr(hAct).c_str()); return 5; }
    Ptr<IAudioClient> c;
    h = unk->QueryInterface(__uuidof(IAudioClient), (void**)&c); unk->Release();
    if (FAILED(h)) { printf("ERROR queryinterface %s\n", hr(h).c_str()); return 6; }
    WAVEFORMATEX w = {}; w.wFormatTag = WAVE_FORMAT_IEEE_FLOAT; w.nChannels = 2; w.nSamplesPerSec = 48000; w.wBitsPerSample = 32;
    w.nBlockAlign = 8; w.nAvgBytesPerSec = 48000 * 8;
    HANDLE evt = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    h = c->Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, 0, 0, &w, nullptr);
    if (FAILED(h)) { printf("ERROR initialize %s\n", hr(h).c_str()); return 7; }
    c->SetEventHandle(evt);
    Ptr<IAudioCaptureClient> cap;
    h = c->GetService(__uuidof(IAudioCaptureClient), (void**)&cap);
    if (FAILED(h)) { printf("ERROR capture service %s\n", hr(h).c_str()); return 8; }
    FILE* df = dump ? fopen(dump, "wb") : nullptr;
    double ss = 0; float pk = 0; ULONGLONG frames = 0, packets = 0, silentPk = 0;
    c->Start();
    ULONGLONG t0 = GetTickCount64(), end = t0 + (ULONGLONG)(secs * 1000), warm = t0 + 500;
    while (GetTickCount64() < end) {
        WaitForSingleObject(evt, 10);
        UINT32 pkt = 0;
        while (SUCCEEDED(cap->GetNextPacketSize(&pkt)) && pkt > 0) {
            BYTE* data = nullptr; UINT32 n = 0; DWORD fl = 0;
            if (FAILED(cap->GetBuffer(&data, &n, &fl, nullptr, nullptr))) break;
            bool silent = (fl & AUDCLNT_BUFFERFLAGS_SILENT) != 0;
            if (GetTickCount64() > warm) {
                packets++; if (silent) silentPk++;
                const float* p = (const float*)data;
                for (UINT32 i = 0; i < n * 2; i++) { float v = silent ? 0.f : p[i]; ss += (double)v * v; if (fabsf(v) > pk) pk = fabsf(v); }
                frames += n;
                if (df) { if (silent) { std::vector<float> z(n * 2, 0.f); fwrite(z.data(), 4, n * 2, df); } else fwrite(p, 4, n * 2, df); }
            }
            cap->ReleaseBuffer(n);
        }
    }
    c->Stop();
    if (df) fclose(df);
    CloseHandle(evt);
    printf("RESULT capture pid=%s mode=%s frames=%llu packets=%llu silent_packets=%llu rms=%.9f peak=%.9f\n", pidS, excl ? "exclude" : "include",
           (unsigned long long)frames, (unsigned long long)packets, (unsigned long long)silentPk, frames ? sqrt(ss / (double)(frames * 2)) : 0.0, pk);
    return 0;
}

// ---------------------------------------------------------------- sessions
static std::string exeOf(DWORD pid) {
    if (!pid) return "(system)";
    HANDLE hp = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!hp) return "?";
    wchar_t buf[MAX_PATH]; DWORD n = MAX_PATH;
    std::string r = "?";
    if (QueryFullProcessImageNameW(hp, 0, buf, &n)) { std::wstring s(buf, n); size_t k = s.find_last_of(L"\\/"); r = U(k == std::wstring::npos ? s : s.substr(k + 1)); }
    CloseHandle(hp);
    return r;
}

template <class Fn>
static int forEachSession(Fn fn) {
    if (!initEnum()) return 0;
    Ptr<IMMDeviceCollection> col;
    if (FAILED(g_enum->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &col))) return 0;
    UINT n = 0; col->GetCount(&n);
    int hits = 0;
    for (UINT i = 0; i < n; i++) {
        Ptr<IMMDevice> d;
        if (FAILED(col->Item(i, &d))) continue;
        Ptr<IAudioSessionManager2> mgr;
        if (FAILED(d->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&mgr))) continue;
        Ptr<IAudioSessionEnumerator> en;
        if (FAILED(mgr->GetSessionEnumerator(&en))) continue;
        int cnt = 0; en->GetCount(&cnt);
        for (int k = 0; k < cnt; k++) {
            Ptr<IAudioSessionControl> sc;
            if (FAILED(en->GetSession(k, &sc))) continue;
            Ptr<IAudioSessionControl2> sc2;
            if (FAILED(sc->QueryInterface(__uuidof(IAudioSessionControl2), (void**)&sc2))) continue;
            Ptr<ISimpleAudioVolume> vol;
            if (FAILED(sc->QueryInterface(__uuidof(ISimpleAudioVolume), (void**)&vol))) continue;
            DWORD pid = 0; sc2->GetProcessId(&pid);
            AudioSessionState st = AudioSessionStateInactive; sc->GetState(&st);
            hits += fn(friendly(d.p), pid, st, vol.p);
        }
    }
    return hits;
}

static int cmdSessions() {
    forEachSession([](const std::wstring& dev, DWORD pid, AudioSessionState st, ISimpleAudioVolume* v) {
        float vol = 0; BOOL mute = FALSE; v->GetMasterVolume(&vol); v->GetMute(&mute);
        printf("SESSION device='%s' pid=%lu exe=%s state=%d volume=%g mute=%d\n", U(dev).c_str(), (unsigned long)pid, exeOf(pid).c_str(), (int)st, vol, (int)mute);
        return 0;
    });
    return 0;
}
static int cmdSetVol(int argc, char** argv, bool mute) {
    const char* pidS = arg(argc, argv, "--pid");
    if (!pidS) { printf("ERROR --pid\n"); return 2; }
    DWORD want = (DWORD)strtoul(pidS, nullptr, 10);
    float vol = (float)atof(arg(argc, argv, "--vol", "1"));
    bool on = flag(argc, argv, "--on");
    int hits = forEachSession([&](const std::wstring&, DWORD pid, AudioSessionState, ISimpleAudioVolume* v) {
        if (pid != want) return 0;
        if (mute) v->SetMute(on ? TRUE : FALSE, nullptr); else v->SetMasterVolume(vol, nullptr);
        return 1;
    });
    printf("RESULT %s pid=%lu sessions=%d %s\n", mute ? "mute" : "setvol", (unsigned long)want, hits, mute ? (on ? "on" : "off") : "");
    return hits ? 0 : 1;
}

// ---------------------------------------------------------------- endpoint volume / mute
static int cmdEndpoint(int argc, char** argv, bool mute) {
    const char* dev = arg(argc, argv, "--device");
    if (!dev) { printf("ERROR --device\n"); return 2; }
    Ptr<IMMDevice> d; std::wstring name;
    if (!findDevice(eRender, W(dev), d, &name)) { printf("ERROR no render device '%s'\n", dev); return 3; }
    Ptr<IAudioEndpointVolume> ev;
    HRESULT h = d->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_ALL, nullptr, (void**)&ev);
    if (FAILED(h)) { printf("ERROR endpoint volume %s\n", hr(h).c_str()); return 4; }
    if (mute) ev->SetMute(flag(argc, argv, "--on") ? TRUE : FALSE, nullptr);
    else ev->SetMasterVolumeLevelScalar((float)atof(arg(argc, argv, "--vol", "1")), nullptr);
    float cur = 0; BOOL m = FALSE; ev->GetMasterVolumeLevelScalar(&cur); ev->GetMute(&m);
    printf("RESULT endpoint device='%s' volume=%.3f mute=%d\n", U(name).c_str(), cur, (int)m);
    return 0;
}

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    if (argc < 2) { printf("usage: audioprobe <list|tone|record|capture|sessions|setvol|mute|epvol|epmute> ...\n"); return 2; }
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    std::string c = argv[1];
    int rc = 2;
    if (c == "list") rc = cmdList();
    else if (c == "tone") rc = cmdTone(argc, argv);
    else if (c == "record") rc = cmdRecord(argc, argv);
    else if (c == "capture") rc = cmdCapture(argc, argv);
    else if (c == "sessions") rc = cmdSessions();
    else if (c == "setvol") rc = cmdSetVol(argc, argv, false);
    else if (c == "mute") rc = cmdSetVol(argc, argv, true);
    else if (c == "epvol") rc = cmdEndpoint(argc, argv, false);
    else if (c == "epmute") rc = cmdEndpoint(argc, argv, true);
    else printf("unknown command %s\n", c.c_str());
    g_enum.reset();
    CoUninitialize();
    return rc;
}
