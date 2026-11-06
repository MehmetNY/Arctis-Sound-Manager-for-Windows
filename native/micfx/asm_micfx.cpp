// asm_micfx.dll - Arctis Sound Manager's microphone effect: a Windows "audio processing object" (APO).
//
// Programs can only record from a *device*. A device of its own needs a signed kernel driver, so ASM does not make one:
// this small effect is attached to the headset's own microphone instead. Windows' audio engine runs it on every block
// of the microphone before any program gets the sound, hands the block to the ASM audio engine through shared memory
// (see fxshared.h), takes the processed block back and passes that on. Discord, OBS, games and the rest just see
// "Microphone (Arctis Nova Pro Wireless)" - the same one device, now with noise cancelling.
//
// The rules this file lives by, because it runs inside audiodg.exe, which every sound on the PC depends on:
//   * the real-time call never allocates, never waits for a lock and never calls anything that can block;
//   * whatever is wrong (no engine, another format, an exception) the sound passes through *untouched*;
//   * it never fails a format negotiation for a format Windows can sensibly ask for.
//
// Build: ci/build_native.ps1 (MSVC, static runtime). Licence: GPL-3.0-or-later, like the rest of the program.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <sddl.h>
#include <unknwn.h>
#include <mmreg.h>
#include <new>
#include <string.h>
#include <stdarg.h>
#include <stdio.h>
#include <audioenginebaseapo.h>
#if __has_include(<audiomediatype.h>)
#include <audiomediatype.h>
#endif
#include "fxshared.h"

#define ASMFX_DLL_BUILD 1u

// {A998FC6D-EC01-4F8A-BB8F-B20F8F4A5808}
static const GUID CLSID_AsmMicFx = {0xa998fc6d, 0xec01, 0x4f8a, {0xbb, 0x8f, 0xb2, 0x0f, 0x8f, 0x4a, 0x58, 0x08}};
// KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
static const GUID kFloatSubtype = {0x00000003, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}};

static volatile LONG g_objects = 0;
static volatile LONG g_instances = 0;

// ── a trace, only when asked for ───────────────────────────────────────────────────────────────────────────────────
// Nothing is written unless the file %SystemRoot%\Temp\asm_micfx_trace.log already exists (create an empty one to turn
// the trace on, delete it to turn it off). It is how a "the effect does not load" report is diagnosed: which of the
// steps Windows takes with the effect happened. Never called from the real-time path except for the very first block.
static void Trace(const char* fmt, ...) {
    wchar_t path[MAX_PATH];
    UINT n = GetSystemWindowsDirectoryW(path, MAX_PATH);
    if (n == 0 || n > MAX_PATH - 40) return;
    wcscat_s(path, L"\\Temp\\asm_micfx_trace.log");
    HANDLE f = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return;
    char line[600];
    SYSTEMTIME t;
    GetLocalTime(&t);
    int k = _snprintf_s(line, sizeof(line), _TRUNCATE, "%02d:%02d:%02d.%03d pid %lu tid %lu  ", t.wHour, t.wMinute,
                        t.wSecond, t.wMilliseconds, GetCurrentProcessId(), GetCurrentThreadId());
    va_list ap;
    va_start(ap, fmt);
    int m = _vsnprintf_s(line + k, sizeof(line) - k - 2, _TRUNCATE, fmt, ap);
    va_end(ap);
    if (m < 0) m = (int)strlen(line + k);
    k += m;
    line[k++] = '\n';
    DWORD written = 0;
    WriteFile(f, line, (DWORD)k, &written, nullptr);
    CloseHandle(f);
}

static const char* Guid(REFGUID g, char (&out)[40]) {
    _snprintf_s(out, sizeof(out), _TRUNCATE, "%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x", g.Data1, g.Data2, g.Data3,
                g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3], g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
    return out;
}

// ── the memory shared with the ASM engine (one per process) ────────────────────────────────────────────────────────

struct Shared {
    HANDLE map = nullptr;
    HANDLE wake = nullptr;
    uint8_t* base = nullptr;
    AsmFxHeader* header = nullptr;
};
static Shared g_shared;
static SRWLOCK g_shared_lock = SRWLOCK_INIT;

static bool OpenShared() {                                    // called from LockForProcess (not a real-time thread)
    AcquireSRWLockExclusive(&g_shared_lock);
    bool ok = g_shared.header != nullptr;
    if (!ok) {
        SECURITY_ATTRIBUTES sa = {sizeof(sa), nullptr, FALSE};
        // SYSTEM, LOCAL SERVICE, administrators and every signed-in user: the engine runs as the person using the PC
        if (ConvertStringSecurityDescriptorToSecurityDescriptorW(L"D:(A;;GA;;;SY)(A;;GA;;;LS)(A;;GA;;;BA)(A;;GA;;;AU)",
                                                                 SDDL_REVISION_1, &sa.lpSecurityDescriptor, nullptr)) {
            HANDLE map = CreateFileMappingW(INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0, ASMFX_MAP_BYTES, ASMFX_MAP_NAME);
            bool created = map && GetLastError() != ERROR_ALREADY_EXISTS;
            HANDLE wake = CreateEventW(&sa, FALSE, FALSE, ASMFX_EVENT_NAME);
            LocalFree(sa.lpSecurityDescriptor);
            uint8_t* base = map ? (uint8_t*)MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, ASMFX_MAP_BYTES) : nullptr;
            if (base && wake) {
                AsmFxHeader* h = (AsmFxHeader*)base;
                if (created || h->magic != ASMFX_MAGIC) {
                    memset(base, 0, ASMFX_OFF_IN_DATA);
                    h->version = ASMFX_VERSION;
                    h->lead_frames = 1024;
                    MemoryBarrier();
                    h->magic = ASMFX_MAGIC;
                }
                if (h->version == ASMFX_VERSION) {
                    g_shared.map = map; g_shared.wake = wake; g_shared.base = base; g_shared.header = h;
                    ok = true;
                }
            }
            if (!ok) {
                if (base) UnmapViewOfFile(base);
                if (map) CloseHandle(map);
                if (wake) CloseHandle(wake);
            }
        }
    }
    ReleaseSRWLockExclusive(&g_shared_lock);
    return ok;
}

static void CloseShared() {
    AcquireSRWLockExclusive(&g_shared_lock);
    if (g_shared.base) UnmapViewOfFile(g_shared.base);
    if (g_shared.map) CloseHandle(g_shared.map);
    if (g_shared.wake) CloseHandle(g_shared.wake);
    g_shared = Shared();
    ReleaseSRWLockExclusive(&g_shared_lock);
}

static inline volatile uint64_t* Counter(uint32_t offset) { return (volatile uint64_t*)(g_shared.base + offset); }
static inline float* Ring(uint32_t offset) { return (float*)(g_shared.base + offset); }

static void RingWrite(float* ring, uint64_t position, const float* src, uint32_t frames, uint32_t ch) {
    uint32_t i = (uint32_t)(position & (ASMFX_RING_FRAMES - 1));
    uint32_t first = frames < ASMFX_RING_FRAMES - i ? frames : ASMFX_RING_FRAMES - i;
    memcpy(ring + (size_t)i * ch, src, (size_t)first * ch * sizeof(float));
    if (frames > first) memcpy(ring, src + (size_t)first * ch, (size_t)(frames - first) * ch * sizeof(float));
}

static void RingRead(const float* ring, uint64_t position, float* dst, uint32_t frames, uint32_t ch) {
    uint32_t i = (uint32_t)(position & (ASMFX_RING_FRAMES - 1));
    uint32_t first = frames < ASMFX_RING_FRAMES - i ? frames : ASMFX_RING_FRAMES - i;
    memcpy(dst, ring + (size_t)i * ch, (size_t)first * ch * sizeof(float));
    if (frames > first) memcpy(dst + (size_t)first * ch, ring, (size_t)(frames - first) * ch * sizeof(float));
}

// ── the effect ─────────────────────────────────────────────────────────────────────────────────────────────────────

class MicFx final : public IAudioProcessingObject, public IAudioProcessingObjectRT,
                    public IAudioProcessingObjectConfiguration, public IAudioSystemEffects {
public:
    MicFx() : m_refs(1) { InterlockedIncrement(&g_objects); Trace("MicFx created"); }
    ~MicFx() { InterlockedDecrement(&g_objects); Trace("MicFx destroyed"); }

    // IUnknown
    STDMETHOD(QueryInterface)(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IAudioProcessingObject))
            *ppv = static_cast<IAudioProcessingObject*>(this);
        else if (riid == __uuidof(IAudioProcessingObjectRT))
            *ppv = static_cast<IAudioProcessingObjectRT*>(this);
        else if (riid == __uuidof(IAudioProcessingObjectConfiguration))
            *ppv = static_cast<IAudioProcessingObjectConfiguration*>(this);
        else if (riid == __uuidof(IAudioSystemEffects))
            *ppv = static_cast<IAudioSystemEffects*>(this);
        else {
            char g[40];
            Trace("QueryInterface refused %s", Guid(riid, g));
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHOD_(ULONG, AddRef)() override { return (ULONG)InterlockedIncrement(&m_refs); }
    STDMETHOD_(ULONG, Release)() override {
        LONG n = InterlockedDecrement(&m_refs);
        if (n == 0) delete this;
        return (ULONG)n;
    }

    // IAudioProcessingObject
    STDMETHOD(Reset)() override { return S_OK; }
    STDMETHOD(GetLatency)(HNSTIME* pTime) override {
        if (!pTime) return E_POINTER;
        *pTime = 0;
        return S_OK;
    }
    STDMETHOD(GetRegistrationProperties)(APO_REG_PROPERTIES** ppRegProps) override {
        if (!ppRegProps) return E_POINTER;
        Trace("GetRegistrationProperties");
        const size_t bytes = sizeof(APO_REG_PROPERTIES) + 3 * sizeof(IID);       // the struct holds one id already
        APO_REG_PROPERTIES* p = (APO_REG_PROPERTIES*)CoTaskMemAlloc(bytes);
        if (!p) return E_OUTOFMEMORY;
        memset(p, 0, bytes);
        p->clsid = CLSID_AsmMicFx;
        p->Flags = (APO_FLAG)(APO_FLAG_DEFAULT | APO_FLAG_INPLACE);
        wcscpy_s(p->szFriendlyName, L"Arctis Sound Manager microphone effect");
        wcscpy_s(p->szCopyrightInfo, L"GPL-3.0-or-later");
        p->u32MajorVersion = 1;
        p->u32MinorVersion = 0;
        p->u32MinInputConnections = 1;
        p->u32MaxInputConnections = 1;
        p->u32MinOutputConnections = 1;
        p->u32MaxOutputConnections = 1;
        p->u32MaxInstances = 0xffffffff;
        p->u32NumAPOInterfaces = 4;
        p->iidAPOInterfaceList[0] = __uuidof(IAudioProcessingObject);
        (&p->iidAPOInterfaceList[0])[1] = __uuidof(IAudioProcessingObjectConfiguration);
        (&p->iidAPOInterfaceList[0])[2] = __uuidof(IAudioProcessingObjectRT);
        (&p->iidAPOInterfaceList[0])[3] = __uuidof(IAudioSystemEffects);          // what marks an APO as a system effect (SFX/MFX/EFX)
        *ppRegProps = p;
        return S_OK;
    }
    STDMETHOD(Initialize)(UINT32 size, BYTE*) override { Trace("Initialize (%u bytes of initialisation data)", size); return S_OK; }
    STDMETHOD(IsInputFormatSupported)(IAudioMediaType* pOpposite, IAudioMediaType* pRequested,
                                      IAudioMediaType** ppSupported) override {
        return CheckFormat(pOpposite, pRequested, ppSupported);
    }
    STDMETHOD(IsOutputFormatSupported)(IAudioMediaType* pOpposite, IAudioMediaType* pRequested,
                                       IAudioMediaType** ppSupported) override {
        return CheckFormat(pOpposite, pRequested, ppSupported);
    }
    STDMETHOD(GetInputChannelCount)(UINT32* pChannels) override {
        if (!pChannels) return E_POINTER;
        *pChannels = m_channels ? m_channels : 1;
        return S_OK;
    }

    // IAudioProcessingObjectConfiguration
    STDMETHOD(LockForProcess)(UINT32 numIn, APO_CONNECTION_DESCRIPTOR** in, UINT32 numOut,
                              APO_CONNECTION_DESCRIPTOR** out) override {
        if (numIn != 1 || numOut != 1 || !in || !out || !in[0] || !in[0]->pFormat) return E_INVALIDARG;
        UNCOMPRESSEDAUDIOFORMAT f = {};
        if (FAILED(in[0]->pFormat->GetUncompressedAudioFormat(&f))) return E_FAIL;
        m_channels = f.dwSamplesPerFrame;
        m_rate = (uint32_t)(f.fFramesPerSecond + 0.5f);
        m_float = f.guidFormatType == kFloatSubtype && f.dwBytesPerSampleContainer == 4;
        m_supported = m_float && m_channels >= 1 && m_channels <= ASMFX_MAX_CHANNELS && m_rate == ASMFX_RATE;
        m_owner = false;
        m_primed = false;
        m_misses = 0;
        const bool shared_ok = OpenShared();
        Trace("LockForProcess: %u channels, %u Hz, float=%d, supported=%d, shared memory %s (error %lu)", m_channels,
              m_rate, (int)m_float, (int)m_supported, shared_ok ? "open" : "NOT open", shared_ok ? 0ul : GetLastError());
        if (shared_ok) {
            AsmFxHeader* h = g_shared.header;
            if (m_id == 0) {
                LONG n = InterlockedIncrement(&g_instances);
                m_id = (GetCurrentProcessId() << 8) ^ (uint32_t)n ^ 0x1u;
                if (m_id == 0) m_id = 1;
            }
            ULONGLONG now = GetTickCount64();
            uint32_t owner = h->instance;
            bool stale = owner != 0 && owner != m_id && now - h->apo_tick_ms > 3000;
            if (owner == 0 || owner == m_id || stale) {
                if (InterlockedCompareExchange((volatile LONG*)&h->instance, (LONG)m_id, (LONG)owner) == (LONG)owner) {
                    m_owner = true;
                    h->apo_pid = GetCurrentProcessId();
                    h->rate = m_rate;
                    h->channels = m_channels;
                    h->dll_build = ASMFX_DLL_BUILD;
                    h->apo_tick_ms = now;
                    // start empty: whatever the engine still had pending belongs to a stream that is gone
                    *Counter(ASMFX_OFF_IN_WRITE) = *Counter(ASMFX_OFF_IN_READ);
                    *Counter(ASMFX_OFF_OUT_READ) = *Counter(ASMFX_OFF_OUT_WRITE);
                    MemoryBarrier();
                    h->generation = h->generation + 1;
                    h->apo_state = m_supported ? ASMFX_STATE_PRIMING : ASMFX_STATE_FORMAT;
                }
            }
            if (!m_owner) h->apo_state = h->apo_state;   // the owner keeps reporting; this instance only passes through
        }
        m_locked = true;
        Trace("LockForProcess done, owner=%d", (int)m_owner);
        return S_OK;
    }
    STDMETHOD(UnlockForProcess)() override {
        Trace("UnlockForProcess");
        m_locked = false;
        if (m_owner && g_shared.header) {
            AsmFxHeader* h = g_shared.header;
            h->apo_state = ASMFX_STATE_IDLE;
            InterlockedCompareExchange((volatile LONG*)&h->instance, 0, (LONG)m_id);
        }
        m_owner = false;
        return S_OK;
    }

    // IAudioProcessingObjectRT
    void STDMETHODCALLTYPE APOProcess(UINT32 numIn, APO_CONNECTION_PROPERTY** in, UINT32 numOut,
                                      APO_CONNECTION_PROPERTY** out) override {
        if (numIn < 1 || numOut < 1 || !in || !out || !in[0] || !out[0]) return;
        const APO_CONNECTION_PROPERTY* ci = in[0];
        APO_CONNECTION_PROPERTY* co = out[0];
        const uint32_t frames = ci->u32ValidFrameCount;
        if (!m_traced) { m_traced = true; Trace("first block: %u frames, flags %u, owner=%d", frames, (unsigned)ci->u32BufferFlags, (int)m_owner); }
        const bool silent = ci->u32BufferFlags == BUFFER_SILENT;
        const float* src = (const float*)ci->pBuffer;
        float* dst = (float*)co->pBuffer;
        co->u32ValidFrameCount = frames;
        co->u32BufferFlags = BUFFER_VALID;
        const uint32_t ch = m_channels ? m_channels : 1;
        if (!src || !dst || frames == 0) return;
        if (!m_float) {                                           // not float: the buffer is not ours to interpret
            if (dst != src && src) memcpy(dst, src, (size_t)frames * ch * 2);
            return;
        }
        __try {
            Process(src, dst, frames, ch, silent);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            m_faulted = true;
            if (g_shared.header) { g_shared.header->fault = GetExceptionCode(); g_shared.header->apo_state = ASMFX_STATE_FAULT; }
            Pass(src, dst, frames, ch, silent);
        }
    }
    UINT32 STDMETHODCALLTYPE CalcInputFrames(UINT32 outputFrames) override { return outputFrames; }
    UINT32 STDMETHODCALLTYPE CalcOutputFrames(UINT32 inputFrames) override { return inputFrames; }

private:
    static void Pass(const float* src, float* dst, uint32_t frames, uint32_t ch, bool silent) {
        const size_t n = (size_t)frames * ch;
        if (silent) memset(dst, 0, n * sizeof(float));
        else if (dst != src) memcpy(dst, src, n * sizeof(float));
    }

    HRESULT CheckFormat(IAudioMediaType* opposite, IAudioMediaType* requested, IAudioMediaType** supported) {
        if (!requested || !supported) return E_POINTER;
        *supported = nullptr;
        UNCOMPRESSEDAUDIOFORMAT f = {};
        if (FAILED(requested->GetUncompressedAudioFormat(&f))) return APOERR_FORMAT_NOT_SUPPORTED;
        Trace("format asked: %u channels, %.0f Hz, %u-byte container, float=%d, opposite=%d", f.dwSamplesPerFrame,
              f.fFramesPerSecond, f.dwBytesPerSampleContainer, (int)(f.guidFormatType == kFloatSubtype), opposite ? 1 : 0);
        // like every effect that works on floating point: 32-bit float samples, anything else is not ours to handle
        if (!(f.guidFormatType == kFloatSubtype && f.dwBytesPerSampleContainer == 4)) return APOERR_FORMAT_NOT_SUPPORTED;
        if (opposite) {
            UNCOMPRESSEDAUDIOFORMAT o = {};
            if (SUCCEEDED(opposite->GetUncompressedAudioFormat(&o)) &&
                (o.dwSamplesPerFrame != f.dwSamplesPerFrame || o.fFramesPerSecond != f.fFramesPerSecond))
                return APOERR_FORMAT_NOT_SUPPORTED;
        }
        *supported = requested;
        requested->AddRef();
        return S_OK;
    }

    void Process(const float* src, float* dst, uint32_t frames, uint32_t ch, bool silent) {
        AsmFxHeader* h = g_shared.header;
        if (!h || !m_owner || m_faulted || !m_supported || frames > ASMFX_RING_FRAMES / 4) {
            Pass(src, dst, frames, ch, silent);
            return;
        }
        const ULONGLONG now = GetTickCount64();
        h->apo_tick_ms = now;
        h->calls = h->calls + 1;
        h->period_frames = frames;
        if (h->instance != m_id) {                                 // somebody else took the rings over
            m_owner = false;
            Pass(src, dst, frames, ch, silent);
            return;
        }
        const bool alive = h->engine_pid != 0 && now - h->engine_tick_ms < 600;
        if (!alive) {
            h->apo_state = ASMFX_STATE_NO_ENGINE;
            m_primed = false;
            Pass(src, dst, frames, ch, silent);
            return;
        }
        if (!h->engine_wants) {
            h->apo_state = ASMFX_STATE_BYPASS;
            m_primed = false;
            Pass(src, dst, frames, ch, silent);
            return;
        }

        // 1. the raw microphone goes to the engine
        float* in_ring = Ring(ASMFX_OFF_IN_DATA);
        const uint64_t iw = *Counter(ASMFX_OFF_IN_WRITE);
        const uint64_t ir = *Counter(ASMFX_OFF_IN_READ);
        uint64_t used = iw >= ir ? iw - ir : 0;
        if (used > ASMFX_RING_FRAMES) used = ASMFX_RING_FRAMES;
        const uint32_t space = ASMFX_RING_FRAMES - (uint32_t)used;
        const uint32_t n = frames < space ? frames : space;
        if (n) {
            if (silent) {
                float zeros[1024];
                uint32_t done = 0;
                memset(zeros, 0, sizeof(zeros));
                const uint32_t per = (uint32_t)(sizeof(zeros) / sizeof(float) / ch);
                while (done < n) {
                    uint32_t k = n - done < per ? n - done : per;
                    RingWrite(in_ring, iw + done, zeros, k, ch);
                    done += k;
                }
            } else {
                RingWrite(in_ring, iw, src, n, ch);
            }
        }
        MemoryBarrier();
        *Counter(ASMFX_OFF_IN_WRITE) = iw + n;
        if (n < frames) h->overruns = h->overruns + (frames - n);
        h->in_frames = h->in_frames + n;
        SetEvent(g_shared.wake);

        // 2. what the engine has finished comes back
        const float* out_ring = Ring(ASMFX_OFF_OUT_DATA);
        const uint64_t ow = *Counter(ASMFX_OFF_OUT_WRITE);
        uint64_t orr = *Counter(ASMFX_OFF_OUT_READ);
        int64_t avail = (int64_t)(ow - orr);
        if (avail < 0 || avail > (int64_t)ASMFX_RING_FRAMES) {      // the engine realigned its side
            orr = ow;
            *Counter(ASMFX_OFF_OUT_READ) = orr;
            avail = 0;
            m_primed = false;
        }
        uint32_t lead = h->lead_frames;
        if (lead < frames) lead = frames;
        if (lead > ASMFX_RING_FRAMES / 2) lead = ASMFX_RING_FRAMES / 2;
        if (!m_primed && avail >= (int64_t)lead) m_primed = true;
        if (!m_primed) {
            h->apo_state = ASMFX_STATE_PRIMING;
            memset(dst, 0, (size_t)frames * ch * sizeof(float));
            return;
        }
        h->apo_state = ASMFX_STATE_PROCESSING;
        const uint32_t got = avail >= (int64_t)frames ? frames : (uint32_t)avail;
        if (got) RingRead(out_ring, orr, dst, got, ch);
        if (got < frames) {
            memset(dst + (size_t)got * ch, 0, (size_t)(frames - got) * ch * sizeof(float));
            h->underruns = h->underruns + 1;
            if (++m_misses > 60) { m_primed = false; m_misses = 0; }   // the engine is not keeping up: start over
        } else {
            m_misses = 0;
        }
        uint64_t next = orr + got;
        const int64_t left = avail - (int64_t)got;
        if (left > (int64_t)lead + 4 * (int64_t)frames) next += (uint64_t)(left - (int64_t)lead);   // a burst: keep the delay bounded
        MemoryBarrier();
        *Counter(ASMFX_OFF_OUT_READ) = next;
        h->out_frames = h->out_frames + got;
    }

    volatile LONG m_refs;
    uint32_t m_id = 0;
    uint32_t m_channels = 0;
    uint32_t m_rate = 0;
    bool m_float = false;
    bool m_supported = false;
    bool m_locked = false;
    bool m_owner = false;
    bool m_primed = false;
    bool m_faulted = false;
    bool m_traced = false;
    uint32_t m_misses = 0;
};

// ── COM boilerplate ────────────────────────────────────────────────────────────────────────────────────────────────

class Factory final : public IClassFactory {
public:
    STDMETHOD(QueryInterface)(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IClassFactory)) { *ppv = static_cast<IClassFactory*>(this); return S_OK; }
        *ppv = nullptr;
        char g[40];
        Trace("factory QueryInterface refused %s", Guid(riid, g));
        return E_NOINTERFACE;
    }
    STDMETHOD_(ULONG, AddRef)() override { return 2; }
    STDMETHOD_(ULONG, Release)() override { return 1; }
    STDMETHOD(CreateInstance)(IUnknown* outer, REFIID riid, void** ppv) override {
        char g[40];
        Trace("CreateInstance %s%s", Guid(riid, g), outer ? " (aggregated)" : "");
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (outer) return CLASS_E_NOAGGREGATION;
        MicFx* fx = new (std::nothrow) MicFx();
        if (!fx) return E_OUTOFMEMORY;
        HRESULT hr = fx->QueryInterface(riid, ppv);
        fx->Release();
        Trace("CreateInstance -> %08lx", (unsigned long)hr);
        return hr;
    }
    STDMETHOD(LockServer)(BOOL lock) override {
        if (lock) InterlockedIncrement(&g_objects); else InterlockedDecrement(&g_objects);
        return S_OK;
    }
};
static Factory g_factory;

extern "C" HRESULT __stdcall DllGetClassObject(REFCLSID clsid, REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    char g[40];
    Trace("DllGetClassObject %08lx for %s", clsid.Data1, Guid(riid, g));
    if (!IsEqualGUID(clsid, CLSID_AsmMicFx)) return CLASS_E_CLASSNOTAVAILABLE;
    HRESULT hr = g_factory.QueryInterface(riid, ppv);
    Trace("DllGetClassObject -> %08lx", (unsigned long)hr);
    return hr;
}

extern "C" HRESULT __stdcall DllCanUnloadNow() { return g_objects == 0 ? S_OK : S_FALSE; }

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        wchar_t exe[MAX_PATH] = L"?";
        GetModuleFileNameW(nullptr, exe, MAX_PATH);
        const wchar_t* base = wcsrchr(exe, L'\\');
        Trace("loaded into %ls", base ? base + 1 : exe);
    }
    if (reason == DLL_PROCESS_DETACH) CloseShared();
    return TRUE;
}
