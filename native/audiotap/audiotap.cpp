// audiotap.cpp - asm-audiotap: the per-application audio tap of the Windows port of Arctis Sound Manager.
//
// Windows mixes every program's sound straight into the headset; there is nothing between the program and
// the speaker that another program may change. What Windows *does* offer is
//   * "process loopback": a copy of one program's sound (and its child processes'), taken AFTER the
//     program's own session volume and BEFORE the endpoint volume / mute (measured on Windows Server 2022/2025:
//     the copy follows the session volume linearly, down to 1e-8, in 32-bit float), and
//   * the session volume of every program on an endpoint.
// asm-audiotap puts the two together:
//
//   1. a program starts playing to the chosen endpoint (the headset): its session volume is lowered to
//      eps (1e-7, -140 dB: below one 24-bit step, so nothing reaches the ear) and the volume it had is
//      remembered;
//   2. a process-loopback copy of the program is taken and multiplied by (remembered volume / eps), which
//      gives back exactly the sound the program meant to play (float arithmetic, no loss);
//   3. the copies are mixed per *channel* (game / chat / media / aux) according to rules by program name;
//   4. the mix is written to stdout, 10 ms at a time, for the ASM audio engine, which equalises, spatialises
//      and plays it to the headset.
//
// Whenever this program stops - the engine closes the pipe, a quit command, Ctrl-C, log-off - every session
// volume is put back. A list of what was lowered is kept on disk so that `--restore` can undo it after a crash.
//
//   asm-audiotap [--endpoint <name piece>|default] [--exclude-pid N]... [--state <file>] [--eps 1e-7]
//   asm-audiotap --restore [--state <file>]
//   asm-audiotap --list            (what is playing where; for diagnostics)
//
// stdin  (tab separated lines):  endpoint<TAB>text | default<TAB>channel|bypass | rule<TAB>exe<TAB>channel|bypass |
//                                 exclude<TAB>pid | apply | quit
// stdout (binary, repeated):     "ATP1" u32 frames u32 channels u32 reserved, then frames*channels float32
// stderr (one JSON per line):    {"t":"status",...}  {"t":"log",...}
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <initguid.h>
#include <windows.h>
#include <mmsystem.h>
#include <fcntl.h>
#include <io.h>
#include <tlhelp32.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <functiondiscoverykeys_devpkey.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwctype>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

// ----------------------------------------------------------------------------------------------- constants
static const int kRate = 48000;
static const int kChunk = 480;                       // frames per output chunk: 10 ms
static const int kChannels = 4;                      // game, chat, media, aux (stereo each)
static const char* kChannelNames[kChannels] = {"game", "chat", "media", "aux"};
static const int kOutChannels = kChannels * 2;
static float g_eps = 2e-8f;

DEFINE_GUID(ASM_IID_IAgileObject, 0x94ea2b94, 0xe9cc, 0x49e0, 0xc0, 0xff, 0xee, 0x64, 0xca, 0x8f, 0x5b, 0x90);
DEFINE_GUID(ASM_IID_ActivateHandler, 0x41d949ab, 0x9862, 0x444a, 0x80, 0xf6, 0xc2, 0x61, 0x33, 0x4d, 0xa5, 0xeb);
DEFINE_GUID(ASM_SUBTYPE_FLOAT, 0x00000003, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);
// the context that marks our own volume changes, so a change made by somebody else can be told apart
DEFINE_GUID(ASM_CONTEXT, 0x6a5d3c71, 0x2e0b, 0x4c52, 0x9b, 0x1e, 0x41, 0xa7, 0x0c, 0x9d, 0x5a, 0x11);

struct AsmLoopbackParams { DWORD TargetProcessId; int Mode; };
struct AsmActivationParams { int ActivationType; AsmLoopbackParams Loopback; };

// ----------------------------------------------------------------------------------------------- helpers
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

static std::wstring W(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring w(n, 0);
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
    w.resize(n - 1);
    return w;
}
static std::string U(const std::wstring& w) {
    if (w.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string s(n, 0);
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, &s[0], n, nullptr, nullptr);
    s.resize(n - 1);
    return s;
}
static std::wstring lc(std::wstring s) { for (auto& c : s) c = (wchar_t)towlower(c); return s; }
static std::string jesc(const std::string& s) {
    std::string o;
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { o += '\\'; o += (char)c; }
        else if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
    return o;
}
static uint64_t nowMs() { return (uint64_t)std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count(); }

static std::mutex g_errMutex;
static void emit(const std::string& json) {
    std::lock_guard<std::mutex> l(g_errMutex);
    fprintf(stderr, "%s\n", json.c_str());
    fflush(stderr);
}
static void logMsg(const std::string& msg) { emit("{\"t\":\"log\",\"msg\":\"" + jesc(msg) + "\"}"); }

static std::wstring friendlyName(IMMDevice* dev) {
    Ptr<IPropertyStore> ps;
    if (FAILED(dev->OpenPropertyStore(STGM_READ, &ps))) return L"?";
    PROPVARIANT v; PropVariantInit(&v);
    std::wstring r = L"?";
    if (SUCCEEDED(ps->GetValue(PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR && v.pwszVal) r = v.pwszVal;
    PropVariantClear(&v);
    return r;
}
static std::wstring deviceId(IMMDevice* dev) {
    LPWSTR id = nullptr; std::wstring r;
    if (SUCCEEDED(dev->GetId(&id)) && id) { r = id; CoTaskMemFree(id); }
    return r;
}

struct ProcInfo { std::wstring exe; std::wstring path; };
static ProcInfo procInfo(DWORD pid) {
    ProcInfo p;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return p;
    wchar_t buf[MAX_PATH * 2]; DWORD n = (DWORD)(sizeof buf / sizeof buf[0]);
    if (QueryFullProcessImageNameW(h, 0, buf, &n)) {
        p.path.assign(buf, n);
        size_t k = p.path.find_last_of(L"\\/");
        p.exe = k == std::wstring::npos ? p.path : p.path.substr(k + 1);
    }
    CloseHandle(h);
    return p;
}
static std::map<DWORD, DWORD> parentMap() {
    std::map<DWORD, DWORD> m;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return m;
    PROCESSENTRY32W e; e.dwSize = sizeof e;
    if (Process32FirstW(snap, &e)) do { m[e.th32ProcessID] = e.th32ParentProcessID; } while (Process32NextW(snap, &e));
    CloseHandle(snap);
    return m;
}

// ----------------------------------------------------------------------------------------------- state file
// One line per program whose volume was lowered:  <full exe path>\t<volume it had>
struct StateFile {
    std::wstring path;
    void write(const std::map<std::wstring, float>& entries) {
        if (path.empty()) return;
        if (entries.empty()) { DeleteFileW(path.c_str()); return; }
        std::wstring tmp = path + L".tmp";
        FILE* f = _wfopen(tmp.c_str(), L"wb");
        if (!f) return;
        for (auto& e : entries) fprintf(f, "%s\t%.9g\n", U(e.first).c_str(), (double)e.second);
        fclose(f);
        MoveFileExW(tmp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING);
    }
    std::map<std::wstring, float> read() {
        std::map<std::wstring, float> m;
        FILE* f = path.empty() ? nullptr : _wfopen(path.c_str(), L"rb");
        if (!f) return m;
        char line[4096];
        while (fgets(line, sizeof line, f)) {
            char* tab = strrchr(line, '\t');
            if (!tab) continue;
            *tab = 0;
            m[lc(W(line))] = (float)atof(tab + 1);
        }
        fclose(f);
        return m;
    }
};

// ----------------------------------------------------------------------------------------------- process loopback
class ActivateHandler : public IActivateAudioInterfaceCompletionHandler {
    LONG ref_ = 1;
public:
    HANDLE done;
    ActivateHandler() { done = CreateEventW(nullptr, TRUE, FALSE, nullptr); }
    virtual ~ActivateHandler() { CloseHandle(done); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID r, void** o) override {
        if (IsEqualIID(r, IID_IUnknown) || IsEqualIID(r, ASM_IID_ActivateHandler) || IsEqualIID(r, ASM_IID_IAgileObject)) {
            *o = static_cast<IActivateAudioInterfaceCompletionHandler*>(this); AddRef(); return S_OK;
        }
        *o = nullptr; return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override { LONG n = InterlockedDecrement(&ref_); if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation*) override { SetEvent(done); return S_OK; }
};

static WAVEFORMATEX floatStereo() {
    WAVEFORMATEX w = {};
    w.wFormatTag = WAVE_FORMAT_IEEE_FLOAT; w.nChannels = 2; w.nSamplesPerSec = kRate; w.wBitsPerSample = 32;
    w.nBlockAlign = 8; w.nAvgBytesPerSec = kRate * 8;
    return w;
}

struct Capture {
    DWORD pid = 0;
    std::wstring exe;
    std::atomic<int> channel{0};
    std::atomic<float> gain{1.0f};                    // remembered volume / eps
    Ptr<IAudioClient> client;
    Ptr<IAudioCaptureClient> cap;
    HANDLE evt = nullptr;
    std::vector<float> fifo;                          // interleaved stereo, only touched by the audio thread
    size_t fifoRead = 0;
    uint64_t leaks = 0, packets = 0;
    std::atomic<bool> dead{false};
    std::atomic<bool> leaked{false};                  // told to the manager: re-apply eps
    double level = 0.0;                               // smoothed output level (RMS) for the status line
    uint64_t startedAt = 0;

    ~Capture() { stop(); }
    void stop() {
        if (client) { client->Stop(); }
        cap.reset(); client.reset();
        if (evt) { CloseHandle(evt); evt = nullptr; }
    }

    bool start(DWORD target, std::string& err) {
        pid = target;
        AsmActivationParams ap = {}; ap.ActivationType = 1; ap.Loopback.TargetProcessId = target; ap.Loopback.Mode = 0;
        PROPVARIANT pv; PropVariantInit(&pv);
        pv.vt = VT_BLOB; pv.blob.cbSize = sizeof ap; pv.blob.pBlobData = (BYTE*)&ap;
        ActivateHandler* hd = new ActivateHandler();
        Ptr<IActivateAudioInterfaceAsyncOperation> op;
        HRESULT h = ActivateAudioInterfaceAsync(L"VAD\\Process_Loopback", __uuidof(IAudioClient), &pv, hd, &op);
        if (FAILED(h)) { err = "ActivateAudioInterfaceAsync failed"; hd->Release(); return false; }
        if (WaitForSingleObject(hd->done, 4000) != WAIT_OBJECT_0) { err = "activation timed out"; hd->Release(); return false; }
        HRESULT act = E_FAIL; IUnknown* unk = nullptr;
        h = op->GetActivateResult(&act, &unk);
        hd->Release();
        if (FAILED(h) || FAILED(act) || !unk) { err = "activation failed"; return false; }
        h = unk->QueryInterface(__uuidof(IAudioClient), (void**)&client);
        unk->Release();
        if (FAILED(h)) { err = "no IAudioClient"; return false; }
        WAVEFORMATEX w = floatStereo();
        evt = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        h = client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                               AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                                   AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                               0, 0, &w, nullptr);
        if (FAILED(h)) { err = "Initialize failed"; return false; }
        client->SetEventHandle(evt);
        h = client->GetService(__uuidof(IAudioCaptureClient), (void**)&cap);
        if (FAILED(h)) { err = "no capture service"; return false; }
        if (FAILED(client->Start())) { err = "Start failed"; return false; }
        startedAt = nowMs();
        return true;
    }

    // Take what has arrived. `leakLimit` is the largest sample that a program at eps can possibly produce; anything
    // louder means the volume is not at eps (a new session, a changed slider) and the packet is thrown away.
    void drain(float leakLimit) {
        if (!cap) return;
        UINT32 n = 0;
        while (SUCCEEDED(cap->GetNextPacketSize(&n)) && n > 0) {
            BYTE* data = nullptr; UINT32 frames = 0; DWORD fl = 0;
            HRESULT h = cap->GetBuffer(&data, &frames, &fl, nullptr, nullptr);
            if (FAILED(h)) { if (h != AUDCLNT_S_BUFFER_EMPTY) dead = true; return; }
            packets++;
            size_t at = fifo.size();
            fifo.resize(at + (size_t)frames * 2);
            if (fl & AUDCLNT_BUFFERFLAGS_SILENT) {
                std::fill(fifo.begin() + at, fifo.end(), 0.0f);
            } else {
                const float* in = (const float*)data;
                float peak = 0.f;
                for (UINT32 i = 0; i < frames * 2; i++) peak = std::max(peak, std::fabs(in[i]));
                if (!(peak <= leakLimit)) {                       // also catches NaN
                    std::fill(fifo.begin() + at, fifo.end(), 0.0f);
                    leaks++; leaked = true;
                } else {
                    float g = gain.load();
                    for (UINT32 i = 0; i < frames * 2; i++) fifo[at + i] = in[i] * g;
                }
            }
            cap->ReleaseBuffer(frames);
        }
    }
    size_t buffered() const { return (fifo.size() - fifoRead) / 2; }
    // Hand out `frames` stereo frames (zero-padded when short) and add them to `bus`.
    void mixInto(float* bus, int frames) {
        size_t have = buffered();
        size_t take = std::min<size_t>(have, (size_t)frames);
        const float* src = fifo.data() + fifoRead;
        double ss = 0;
        for (size_t i = 0; i < take * 2; i++) { bus[i] += src[i]; ss += (double)src[i] * src[i]; }
        fifoRead += take * 2;
        // a backlog of more than 60 ms means this clock runs faster than ours: keep 20 ms
        if (buffered() > (size_t)(kRate * 60 / 1000)) fifoRead = fifo.size() - (size_t)(kRate * 20 / 1000) * 2;
        if (fifoRead > 16384) { fifo.erase(fifo.begin(), fifo.begin() + fifoRead); fifoRead = 0; }
        double rms = take ? std::sqrt(ss / (double)(take * 2)) : 0.0;
        level += 0.05 * (rms - level);
    }
};

// ----------------------------------------------------------------------------------------------- the tap
// What one session tells us as it happens (on audio-service threads, so nothing here may block or touch shared
// state): somebody moved its volume -> wake the manager at once; it is about to disappear -> give the program its
// volume back right now, because Windows remembers the volume a program had when its session ended and the next
// launch (maybe with this program not running) would otherwise start at -160 dB.
class SessionEvents : public IAudioSessionEvents {
    LONG ref_ = 1;
public:
    std::atomic<float> v0{1.0f};
    std::atomic<bool> armed{false};                    // the session is turned down by us
    std::atomic<bool> restoredOnExit{false};
    ISimpleAudioVolume* vol = nullptr;                 // not owned: valid until the notification is unregistered
    std::function<void()> wake;
    virtual ~SessionEvents() {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID r, void** o) override {
        if (IsEqualIID(r, IID_IUnknown) || IsEqualIID(r, __uuidof(IAudioSessionEvents))) { *o = static_cast<IAudioSessionEvents*>(this); AddRef(); return S_OK; }
        *o = nullptr; return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override { LONG n = InterlockedDecrement(&ref_); if (!n) delete this; return n; }
    void restoreNow() {
        if (armed.exchange(false) && vol) { vol->SetMasterVolume(v0.load(), (LPCGUID)&ASM_CONTEXT); restoredOnExit = true; }
    }
    HRESULT STDMETHODCALLTYPE OnDisplayNameChanged(LPCWSTR, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnIconPathChanged(LPCWSTR, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnSimpleVolumeChanged(float, BOOL, LPCGUID ctx) override {
        if (!ctx || !IsEqualGUID(*ctx, ASM_CONTEXT)) { if (wake) wake(); }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnChannelVolumeChanged(DWORD, float[], DWORD, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnGroupingParamChanged(LPCGUID, LPCGUID) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnStateChanged(AudioSessionState st) override {
        if (st == AudioSessionStateExpired) restoreNow();
        if (wake) wake();
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnSessionDisconnected(AudioSessionDisconnectReason) override { restoreNow(); if (wake) wake(); return S_OK; }
};

struct Session {
    std::wstring id;                                  // endpoint id + session instance id
    DWORD pid = 0;
    std::wstring exe, path;
    Ptr<IAudioSessionControl> ctl;
    Ptr<ISimpleAudioVolume> vol;
    SessionEvents* events = nullptr;
    int channel = -1;                                 // -1: not ours (bypass / excluded)
    bool lowered = false;
    float v0 = 1.0f;                                  // the volume the program had
    bool seen = false;
    uint64_t lastSet = 0;
    Session() {}
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    ~Session() {
        if (events) {
            if (ctl) ctl->UnregisterAudioSessionNotification(events);
            events->vol = nullptr;
            events->Release();
        }
    }
};

class Notifier : public IAudioSessionNotification {
    LONG ref_ = 1;
public:
    std::function<void()> wake;
    virtual ~Notifier() {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID r, void** o) override {
        if (IsEqualIID(r, IID_IUnknown) || IsEqualIID(r, __uuidof(IAudioSessionNotification))) { *o = static_cast<IAudioSessionNotification*>(this); AddRef(); return S_OK; }
        *o = nullptr; return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override { LONG n = InterlockedDecrement(&ref_); if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE OnSessionCreated(IAudioSessionControl*) override { if (wake) wake(); return S_OK; }
};

class Tap {
public:
    // configuration (changed from the stdin thread, applied by the manager thread)
    std::mutex cfgMutex;
    std::wstring endpointQuery = L"default";
    std::map<std::wstring, int> rules;                // lower exe -> channel (-1 bypass)
    int defaultChannel = 0;
    std::set<DWORD> excludePids;
    std::atomic<bool> configDirty{true};
    std::atomic<bool> quit{false};
    std::atomic<bool> engaged{false};                 // false: touch nothing (the engine is not playing to the headset yet)
    StateFile state;

    Tap() {}

    int run();
    void requestWake() { { std::lock_guard<std::mutex> l(wakeMutex); wakeFlag = true; } wakeCv.notify_all(); }

private:
    std::mutex wakeMutex; std::condition_variable wakeCv; bool wakeFlag = false;
    Ptr<IMMDeviceEnumerator> enumerator;
    Ptr<IMMDevice> endpoint;
    std::wstring endpointId, endpointName;
    Ptr<IAudioSessionManager2> manager;
    Notifier* notifier = nullptr;
    std::map<std::wstring, std::unique_ptr<Session>> sessions;
    std::map<std::wstring, float> remembered;         // exe path -> the volume it had before we turned it down (kept in the state file)
    std::mutex capMutex;
    std::vector<std::shared_ptr<Capture>> captures;
    std::map<DWORD, uint64_t> captureRetry;
    // output
    std::mutex outMutex; std::condition_variable outCv; std::deque<std::vector<float>> outQueue;
    std::atomic<bool> audioRun{false};
    uint64_t lastStatus = 0;
    std::string lastAppsJson;

    bool resolveEndpoint();
    void releaseEndpoint();
    void refresh();
    int channelFor(const std::wstring& exe, DWORD pid, const std::set<DWORD>& excl, const std::map<std::wstring, int>& rl, int def);
    void attenuate(Session& s);
    void release(Session& s, bool vanished = false);
    void restoreAll();
    void reconcileCaptures();
    void saveState();
    void audioThread();
    void writerThread();
    void status();
};

bool Tap::resolveEndpoint() {
    std::wstring query;
    { std::lock_guard<std::mutex> l(cfgMutex); query = endpointQuery; }
    Ptr<IMMDevice> found; std::wstring name, id;
    if (lc(query) == L"default" || query.empty()) {
        if (FAILED(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &found))) return false;
        name = friendlyName(found.p); id = deviceId(found.p);
    } else {
        Ptr<IMMDeviceCollection> col;
        if (FAILED(enumerator->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &col))) return false;
        UINT n = 0; col->GetCount(&n);
        size_t best = (size_t)-1;
        for (UINT i = 0; i < n; i++) {
            Ptr<IMMDevice> d;
            if (FAILED(col->Item(i, &d))) continue;
            std::wstring f = friendlyName(d.p);
            if (lc(f).find(lc(query)) == std::wstring::npos) continue;
            if (f.size() < best) { best = f.size(); found.reset(); found.p = d.p; d.p = nullptr; name = f; id = deviceId(found.p); }
        }
        if (!found) return false;
    }
    if (id == endpointId && manager) return true;
    releaseEndpoint();
    endpoint.p = found.p; found.p = nullptr;
    endpointId = id; endpointName = name;
    if (FAILED(endpoint->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&manager))) { releaseEndpoint(); return false; }
    notifier = new Notifier();
    notifier->wake = [this] { requestWake(); };
    manager->RegisterSessionNotification(notifier);
    { Ptr<IAudioSessionEnumerator> first; manager->GetSessionEnumerator(&first); }     // notifications start only after one read
    logMsg("endpoint: " + U(endpointName));
    return true;
}

void Tap::releaseEndpoint() {
    restoreAll();
    if (manager && notifier) manager->UnregisterSessionNotification(notifier);
    if (notifier) { notifier->Release(); notifier = nullptr; }
    manager.reset(); endpoint.reset();
    endpointId.clear(); endpointName.clear();
}

int Tap::channelFor(const std::wstring& exe, DWORD pid, const std::set<DWORD>& excl, const std::map<std::wstring, int>& rl, int def) {
    if (pid == 0 || pid == GetCurrentProcessId() || excl.count(pid)) return -1;
    std::wstring e = lc(exe);
    if (e.empty()) return -1;                          // a process we may not open (protected): leave it alone
    auto it = rl.find(e);
    return it != rl.end() ? it->second : def;
}

void Tap::attenuate(Session& s) {
    float v = 1.0f; BOOL m = FALSE;
    s.vol->GetMasterVolume(&v); s.vol->GetMute(&m);
    float v0 = v;
    if (v <= g_eps * 4.0f) {                           // already low: Windows kept the volume of an earlier, turned-down run
        auto it = remembered.find(lc(s.path));
        v0 = it != remembered.end() ? it->second : 1.0f;
    }
    s.v0 = v0;
    remembered[lc(s.path)] = v0;
    saveState();                                       // written BEFORE the change: a crash right now is still recoverable
    if (s.events) { s.events->v0 = v0; s.events->armed = true; }
    s.vol->SetMasterVolume(g_eps, (LPCGUID)&ASM_CONTEXT);
    s.lowered = true; s.lastSet = nowMs();
}

void Tap::release(Session& s, bool vanished) {
    if (!s.lowered) return;
    bool gone = s.events && s.events->restoredOnExit.load();
    if (s.events) s.events->armed = false;
    if (s.vol && !gone) s.vol->SetMasterVolume(s.v0, (LPCGUID)&ASM_CONTEXT);
    s.lowered = false;
    if (vanished && !gone) return;                     // it ended without giving us the chance: Windows may have kept -160 dB, so keep the memory
    // the program's volume is its own again: forget it, unless another session of the same program is still turned down
    for (auto& kv : sessions) if (kv.second.get() != &s && kv.second->lowered && lc(kv.second->path) == lc(s.path)) return;
    remembered.erase(lc(s.path));
    saveState();
}

void Tap::saveState() { state.write(remembered); }

void Tap::restoreAll() {
    for (auto& kv : sessions) release(*kv.second);
    sessions.clear();
    std::lock_guard<std::mutex> l(capMutex);
    captures.clear();
}

void Tap::refresh() {
    std::map<std::wstring, int> rl; int def; std::set<DWORD> excl;
    { std::lock_guard<std::mutex> l(cfgMutex); rl = rules; def = defaultChannel; excl = excludePids; }
    if (!engaged) { if (manager || !sessions.empty()) releaseEndpoint(); return; }   // volumes back at once
    if (!resolveEndpoint()) return;
    Ptr<IAudioSessionEnumerator> en;
    if (FAILED(manager->GetSessionEnumerator(&en))) return;
    int count = 0; en->GetCount(&count);
    for (auto& kv : sessions) kv.second->seen = false;
    for (int i = 0; i < count; i++) {
        Ptr<IAudioSessionControl> ctl;
        if (FAILED(en->GetSession(i, &ctl))) continue;
        Ptr<IAudioSessionControl2> c2;
        if (FAILED(ctl->QueryInterface(__uuidof(IAudioSessionControl2), (void**)&c2))) continue;
        DWORD pid = 0; c2->GetProcessId(&pid);
        LPWSTR inst = nullptr; std::wstring key;
        if (SUCCEEDED(c2->GetSessionInstanceIdentifier(&inst)) && inst) { key = inst; CoTaskMemFree(inst); }
        if (key.empty()) key = L"pid" + std::to_wstring(pid);
        auto it = sessions.find(key);
        if (it == sessions.end()) {
            auto s = std::make_unique<Session>();
            s->id = key; s->pid = pid;
            ProcInfo pi = procInfo(pid);
            s->exe = pi.exe; s->path = pi.path;
            s->ctl.p = ctl.p; ctl.p = nullptr;
            if (FAILED(s->ctl->QueryInterface(__uuidof(ISimpleAudioVolume), (void**)&s->vol))) continue;
            s->events = new SessionEvents();
            s->events->vol = s->vol.p;
            s->events->wake = [this] { requestWake(); };
            if (FAILED(s->ctl->RegisterAudioSessionNotification(s->events))) { s->events->Release(); s->events = nullptr; }
            it = sessions.emplace(key, std::move(s)).first;
        }
        Session& s = *it->second;
        s.seen = true;
        int ch = channelFor(s.exe, s.pid, excl, rl, def);
        if (ch != s.channel) {
            if (s.lowered && ch == -1) release(s);
            s.channel = ch;
        }
        if (s.channel != -1) {
            if (!s.lowered) attenuate(s);
            else {
                // somebody (the user at the volume mixer, the program itself) changed the volume: that is the new
                // "volume the program wants"; remember it and go back to eps at once
                float v = 1.0f; s.vol->GetMasterVolume(&v);
                if (std::fabs(v - g_eps) > g_eps * 0.5f && v > g_eps * 4.0f) {
                    s.v0 = v; remembered[lc(s.path)] = v; saveState();
                    if (s.events) s.events->v0 = v;
                    s.vol->SetMasterVolume(g_eps, (LPCGUID)&ASM_CONTEXT);
                    s.lastSet = nowMs();
                }
            }
        }
    }
    for (auto it = sessions.begin(); it != sessions.end();) {
        if (!it->second->seen) { release(*it->second, true); it = sessions.erase(it); }   // the program is gone (or moved to another device)
        else ++it;
    }
    reconcileCaptures();
}

void Tap::reconcileCaptures() {
    // wanted: one capture per "root" session pid (no managed ancestor), carrying that session's channel
    std::map<DWORD, Session*> managed;
    for (auto& kv : sessions) if (kv.second->channel != -1 && kv.second->lowered) managed[kv.second->pid] = kv.second.get();
    std::set<DWORD> pids; for (auto& kv : managed) pids.insert(kv.first);
    std::map<DWORD, DWORD> parents = managed.size() > 1 ? parentMap() : std::map<DWORD, DWORD>();
    std::map<DWORD, Session*> roots;
    for (auto& kv : managed) {
        bool covered = false;
        if (managed.size() > 1) {
            DWORD cur = kv.first;
            for (int i = 0; i < 32 && !covered; i++) {
                auto it = parents.find(cur);
                if (it == parents.end() || it->second == 0 || it->second == cur) break;
                cur = it->second;
                auto anc = managed.find(cur);
                if (anc != managed.end() && anc->second->channel == kv.second->channel) covered = true;
            }
        }
        if (!covered) roots[kv.first] = kv.second;
    }
    std::lock_guard<std::mutex> l(capMutex);
    for (auto it = captures.begin(); it != captures.end();) {
        auto r = roots.find((*it)->pid);
        if ((*it)->dead || r == roots.end()) { it = captures.erase(it); continue; }
        (*it)->channel = r->second->channel;
        // gain = volume the program wanted / eps; several sessions of one pid: the loudest wanted volume
        float v0 = 0.0f;
        for (auto& kv : sessions) if (kv.second->pid == r->first && kv.second->lowered) v0 = std::max(v0, kv.second->v0);
        (*it)->gain = v0 / g_eps;
        if ((*it)->leaked.exchange(false)) {          // a packet louder than eps allows: some session of this tree is not at eps
            for (auto& kv : sessions) if (kv.second->lowered) { kv.second->vol->SetMasterVolume(g_eps, (LPCGUID)&ASM_CONTEXT); }
        }
        ++it;
    }
    for (auto& kv : roots) {
        bool have = false;
        for (auto& c : captures) if (c->pid == kv.first) { have = true; break; }
        if (have) continue;
        auto retry = captureRetry.find(kv.first);
        if (retry != captureRetry.end() && nowMs() < retry->second) continue;
        auto c = std::make_shared<Capture>();
        std::string err;
        c->exe = kv.second->exe;
        c->channel = kv.second->channel;
        float v0 = 0.0f;
        for (auto& s : sessions) if (s.second->pid == kv.first && s.second->lowered) v0 = std::max(v0, s.second->v0);
        c->gain = v0 / g_eps;
        if (c->start(kv.first, err)) {
            captures.push_back(c);
            captureRetry.erase(kv.first);
            logMsg("tapping " + U(kv.second->exe) + " (pid " + std::to_string(kv.first) + ") -> " + kChannelNames[kv.second->channel]);
        } else {
            captureRetry[kv.first] = nowMs() + 1500;
            logMsg("cannot tap " + U(kv.second->exe) + ": " + err);
        }
    }
}

void Tap::status() {
    std::string apps = "[";
    bool first = true;
    {
        std::lock_guard<std::mutex> l(capMutex);
        for (auto& kv : sessions) {
            Session& s = *kv.second;
            double level = 0; uint64_t leaks = 0;
            for (auto& c : captures) if (c->pid == s.pid) { level = c->level; leaks = c->leaks; }
            char b[64];
            AudioSessionState st = AudioSessionStateInactive;
            if (s.ctl) s.ctl->GetState(&st);
            if (!first) apps += ",";
            first = false;
            apps += "{\"exe\":\"" + jesc(U(s.exe)) + "\",\"pid\":" + std::to_string(s.pid) + ",\"channel\":\"" +
                    (s.channel >= 0 ? kChannelNames[s.channel] : "bypass") + "\",\"managed\":" + (s.lowered ? "true" : "false") +
                    ",\"active\":" + (st == AudioSessionStateActive ? "true" : "false");
            snprintf(b, sizeof b, ",\"level\":%.5f,\"leaks\":%llu}", level, (unsigned long long)leaks);
            apps += b;
        }
    }
    apps += "]";
    if (nowMs() - lastStatus >= 250) {                  // four a second is plenty for a level meter and an app list
        lastAppsJson = apps;
        lastStatus = nowMs();
        emit(std::string("{\"t\":\"status\",\"engaged\":") + (engaged ? "true" : "false") + ",\"endpoint\":\"" + jesc(U(endpointName)) +
             "\",\"eps\":" + std::to_string((double)g_eps) + ",\"apps\":" + apps + "}");
    }
}

// ------------------------------------------------------------------------------------------ audio + output threads
void Tap::audioThread() {
    // 10 ms chunks on a steady clock; whatever the captures delivered is mixed in, silence otherwise
    using clk = std::chrono::steady_clock;
    auto start = clk::now();
    uint64_t produced = 0;
    std::vector<float> bus((size_t)kChannels * kChunk * 2);
    while (audioRun) {
        std::vector<std::shared_ptr<Capture>> snap;
        { std::lock_guard<std::mutex> l(capMutex); snap = captures; }
        float limit = g_eps * 16.0f;
        for (auto& c : snap) c->drain(limit);
        uint64_t due = (uint64_t)(std::chrono::duration<double>(clk::now() - start).count() * kRate / kChunk);
        int made = 0;
        while (produced < due && made < 20) {
            std::fill(bus.begin(), bus.end(), 0.0f);
            std::vector<float> tmp((size_t)kChunk * 2);
            for (auto& c : snap) {
                int ch = c->channel.load();
                if (ch < 0 || ch >= kChannels) continue;
                std::fill(tmp.begin(), tmp.end(), 0.0f);
                c->mixInto(tmp.data(), kChunk);
                for (int i = 0; i < kChunk; i++) {
                    bus[(size_t)i * kOutChannels + ch * 2] += tmp[(size_t)i * 2];
                    bus[(size_t)i * kOutChannels + ch * 2 + 1] += tmp[(size_t)i * 2 + 1];
                }
            }
            for (float& x : bus) { if (!(x == x)) x = 0.f; else if (x > 8.f) x = 8.f; else if (x < -8.f) x = -8.f; }
            std::vector<float> packet(4 + bus.size());
            uint32_t hdr[4] = {0x31505441u /* "ATP1" */, (uint32_t)kChunk, (uint32_t)kOutChannels, 0};
            memcpy(packet.data(), hdr, sizeof hdr);
            memcpy(packet.data() + 4, bus.data(), bus.size() * sizeof(float));
            { std::lock_guard<std::mutex> l(outMutex); outQueue.push_back(std::move(packet)); while (outQueue.size() > 60) outQueue.pop_front(); }
            outCv.notify_one();
            produced++; made++;
        }
        if (produced + 100 < due) produced = due;          // asleep for a second or more (suspend): do not catch up
        Sleep(3);
    }
}

void Tap::writerThread() {
    while (audioRun) {
        std::vector<float> packet;
        {
            std::unique_lock<std::mutex> l(outMutex);
            outCv.wait_for(l, std::chrono::milliseconds(100), [this] { return !outQueue.empty() || !audioRun; });
            if (outQueue.empty()) continue;
            packet = std::move(outQueue.front()); outQueue.pop_front();
        }
        size_t bytes = packet.size() * sizeof(float);
        if (fwrite(packet.data(), 1, bytes, stdout) != bytes) { quit = true; requestWake(); return; }   // the reader went away
        fflush(stdout);
    }
}

static BOOL WINAPI ctrlHandler(DWORD) ;
static Tap* g_tap = nullptr;
static BOOL WINAPI ctrlHandler(DWORD) {
    if (g_tap) { g_tap->quit = true; g_tap->requestWake(); }
    Sleep(1500);                                          // the process ends when the handler returns: give run() time to restore
    return TRUE;
}

int Tap::run() {
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&enumerator))) {
        logMsg("no MMDevice enumerator");
        return 2;
    }
    remembered = state.read();                            // programs whose volume an earlier run (or crash) left turned down
    audioRun = true;
    std::thread audio([this] { audioThread(); });
    std::thread writer([this] { writerThread(); });
    // stdin: commands; end of input = the engine is gone
    std::thread input([this] {
        char line[8192];
        while (fgets(line, sizeof line, stdin)) {
            std::string s(line);
            while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
            std::vector<std::string> t; size_t p = 0;
            for (;;) { size_t q = s.find('\t', p); t.push_back(s.substr(p, q == std::string::npos ? q : q - p)); if (q == std::string::npos) break; p = q + 1; }
            auto chan = [](const std::string& n) { for (int i = 0; i < kChannels; i++) if (n == kChannelNames[i]) return i; return -1; };
            std::lock_guard<std::mutex> l(cfgMutex);
            if (t[0] == "quit") { quit = true; }
            else if (t[0] == "endpoint" && t.size() > 1) { endpointQuery = W(t[1]); configDirty = true; }
            else if (t[0] == "default" && t.size() > 1) { defaultChannel = chan(t[1]); configDirty = true; }
            else if (t[0] == "rule" && t.size() > 2) { rules[lc(W(t[1]))] = chan(t[2]); configDirty = true; }
            else if (t[0] == "clear") { rules.clear(); excludePids.clear(); configDirty = true; }
            else if (t[0] == "exclude" && t.size() > 1) { excludePids.insert((DWORD)strtoul(t[1].c_str(), nullptr, 10)); configDirty = true; }
            else if (t[0] == "apply") { configDirty = true; }
            else if (t[0] == "engage" && t.size() > 1) { engaged = t[1] == "1"; configDirty = true; }
            requestWake();
            if (quit) break;
        }
        quit = true;
        requestWake();
    });
    uint64_t lastRefresh = 0;
    while (!quit) {
        {
            std::unique_lock<std::mutex> l(wakeMutex);
            wakeCv.wait_for(l, std::chrono::milliseconds(50), [this] { return wakeFlag || quit.load(); });
            wakeFlag = false;
        }
        if (quit) break;
        uint64_t now = nowMs();
        if (configDirty.exchange(false) || now - lastRefresh >= 50) {
            lastRefresh = now;
            refresh();
            status();
        }
    }
    releaseEndpoint();                                    // puts every volume back
    audioRun = false;
    outCv.notify_all();
    audio.join();
    writer.join();
    if (input.joinable()) { input.detach(); }
    return 0;
}

// ------------------------------------------------------------------------------------------ one-shot modes
static int restoreMode(StateFile& sf) {
    auto saved = sf.read();
    if (saved.empty()) { logMsg("nothing to restore"); return 0; }
    Ptr<IMMDeviceEnumerator> en;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&en))) return 2;
    Ptr<IMMDeviceCollection> col;
    if (FAILED(en->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &col))) return 2;
    UINT n = 0; col->GetCount(&n);
    int fixed = 0;
    for (UINT i = 0; i < n; i++) {
        Ptr<IMMDevice> d; Ptr<IAudioSessionManager2> mgr; Ptr<IAudioSessionEnumerator> se;
        if (FAILED(col->Item(i, &d)) || FAILED(d->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&mgr)) ||
            FAILED(mgr->GetSessionEnumerator(&se))) continue;
        int cnt = 0; se->GetCount(&cnt);
        for (int k = 0; k < cnt; k++) {
            Ptr<IAudioSessionControl> sc; Ptr<IAudioSessionControl2> sc2; Ptr<ISimpleAudioVolume> v;
            if (FAILED(se->GetSession(k, &sc)) || FAILED(sc->QueryInterface(__uuidof(IAudioSessionControl2), (void**)&sc2)) ||
                FAILED(sc->QueryInterface(__uuidof(ISimpleAudioVolume), (void**)&v))) continue;
            DWORD pid = 0; sc2->GetProcessId(&pid);
            auto it = saved.find(lc(procInfo(pid).path));
            if (it == saved.end()) continue;
            float cur = 1.0f; v->GetMasterVolume(&cur);
            if (cur < 1e-3f) { v->SetMasterVolume(it->second, (LPCGUID)&ASM_CONTEXT); fixed++; }
        }
    }
    sf.write({});
    logMsg("restored " + std::to_string(fixed) + " session volume(s)");
    return 0;
}

static int listMode() {
    Ptr<IMMDeviceEnumerator> en;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&en))) return 2;
    Ptr<IMMDeviceCollection> col;
    if (FAILED(en->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &col))) return 2;
    UINT n = 0; col->GetCount(&n);
    Ptr<IMMDevice> def; std::wstring defId;
    if (SUCCEEDED(en->GetDefaultAudioEndpoint(eRender, eConsole, &def))) defId = deviceId(def.p);
    for (UINT i = 0; i < n; i++) {
        Ptr<IMMDevice> d; Ptr<IAudioSessionManager2> mgr; Ptr<IAudioSessionEnumerator> se;
        if (FAILED(col->Item(i, &d))) continue;
        printf("ENDPOINT %s%s\n", U(friendlyName(d.p)).c_str(), deviceId(d.p) == defId ? " [default]" : "");
        if (FAILED(d->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&mgr)) || FAILED(mgr->GetSessionEnumerator(&se))) continue;
        int cnt = 0; se->GetCount(&cnt);
        for (int k = 0; k < cnt; k++) {
            Ptr<IAudioSessionControl> sc; Ptr<IAudioSessionControl2> sc2; Ptr<ISimpleAudioVolume> v;
            if (FAILED(se->GetSession(k, &sc)) || FAILED(sc->QueryInterface(__uuidof(IAudioSessionControl2), (void**)&sc2)) ||
                FAILED(sc->QueryInterface(__uuidof(ISimpleAudioVolume), (void**)&v))) continue;
            DWORD pid = 0; sc2->GetProcessId(&pid);
            AudioSessionState st = AudioSessionStateInactive; sc->GetState(&st);
            float vol = 0; BOOL mute = FALSE; v->GetMasterVolume(&vol); v->GetMute(&mute);
            printf("  SESSION pid=%lu exe=%s state=%d volume=%g mute=%d\n", (unsigned long)pid, U(procInfo(pid).exe).c_str(), (int)st, vol, (int)mute);
        }
    }
    return 0;
}

int main(int argc, char** argv) {
    _setmode(_fileno(stdout), _O_BINARY);
    _setmode(_fileno(stdin), _O_BINARY);
    setvbuf(stdout, nullptr, _IOFBF, 1 << 16);
    timeBeginPeriod(1);                                    // Sleep(3) must mean 3 ms, not 15
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);

    Tap tap;
    bool doRestore = false, doList = false;
    std::wstring statePath;
    {
        wchar_t la[MAX_PATH];
        DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", la, MAX_PATH);
        if (n > 0 && n < MAX_PATH) statePath = std::wstring(la) + L"\\arctis_manager\\run\\audiotap_state.txt";
    }
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&](void) -> std::string { return i + 1 < argc ? std::string(argv[++i]) : std::string(); };
        if (a == "--restore") doRestore = true;
        else if (a == "--list") doList = true;
        else if (a == "--endpoint") tap.endpointQuery = W(next());
        else if (a == "--exclude-pid") tap.excludePids.insert((DWORD)strtoul(next().c_str(), nullptr, 10));
        else if (a == "--state") statePath = W(next());
        else if (a == "--eps") g_eps = (float)atof(next().c_str());
    }
    tap.state.path = statePath;
    if (!statePath.empty()) {                              // make sure the directory exists
        size_t k = statePath.find_last_of(L"\\/");
        if (k != std::wstring::npos) {
            std::wstring dir = statePath.substr(0, k), cur;
            for (size_t i = 0; i <= dir.size(); i++) {
                if (i == dir.size() || dir[i] == L'\\' || dir[i] == L'/') { if (cur.size() > 2) CreateDirectoryW(cur.c_str(), nullptr); }
                if (i < dir.size()) cur += dir[i];
            }
        }
    }
    int rc;
    if (doList) rc = listMode();
    else if (doRestore) rc = restoreMode(tap.state);
    else {
        g_tap = &tap;
        SetConsoleCtrlHandler(ctrlHandler, TRUE);
        if (!tap.excludePids.empty()) {}
        rc = tap.run();
    }
    fflush(stdout);
    CoUninitialize();
    timeEndPeriod(1);
    return rc;
}
