// The memory the microphone effect (asm_micfx.dll, inside Windows' audio engine) and the ASM audio engine share.
// Mirrored in src/arctis_sound_manager/win/fx/shared.py - change both together and bump ASMFX_VERSION.
//
// Windows' audio engine (audiodg.exe) runs the effect in session 0 as LOCAL SERVICE; the ASM engine runs as the person
// using the PC. The effect therefore *creates* the named objects (only a service may create "Global\" objects) and the
// engine opens them. Four counters decide who writes what, and each is written by exactly one side:
//
//   in_write  effect   frames the effect has put into the input ring (the raw microphone)
//   in_read   engine   frames the engine has taken out of it
//   out_write engine   frames the engine has put into the output ring (the processed microphone)
//   out_read  effect   frames the effect has taken out of it
//
// All counters only ever grow (frames since the rings were last reset); the ring position is counter % ASMFX_RING_FRAMES.
#pragma once
#include <stdint.h>

#define ASMFX_MAGIC        0x58464D41u        // "AMFX"
#define ASMFX_VERSION      1u
#define ASMFX_MAP_NAME     L"Global\\ASM_MicFx_v1"
#define ASMFX_EVENT_NAME   L"Global\\ASM_MicFx_Wake_v1"
#define ASMFX_RING_FRAMES  16384u             // per ring, a power of two (341 ms at 48 kHz)
#define ASMFX_MAX_CHANNELS 2u
#define ASMFX_RATE         48000u             // the only rate the engine's microphone chain works at

// apo_state
#define ASMFX_STATE_IDLE            0u        // no stream is running through the effect
#define ASMFX_STATE_PRIMING         1u        // the engine is alive; waiting until the output ring holds `lead_frames`
#define ASMFX_STATE_PROCESSING      2u        // the microphone goes to the engine and comes back
#define ASMFX_STATE_NO_ENGINE       3u        // passthrough: no engine is running (or it stopped answering)
#define ASMFX_STATE_BYPASS          4u        // passthrough: the engine asked for it (nothing to process)
#define ASMFX_STATE_FORMAT          5u        // passthrough: not 48 kHz mono/stereo float
#define ASMFX_STATE_FAULT           6u        // passthrough: the effect met an error in this process and gave up
#define ASMFX_STATE_NOT_OWNER       7u        // passthrough: another instance owns the rings

#define ASMFX_OFF_HEADER   0u
#define ASMFX_OFF_IN_WRITE 256u
#define ASMFX_OFF_IN_READ  320u
#define ASMFX_OFF_OUT_WRITE 384u
#define ASMFX_OFF_OUT_READ 448u
#define ASMFX_OFF_IN_DATA  512u
#define ASMFX_OFF_OUT_DATA (ASMFX_OFF_IN_DATA + ASMFX_RING_FRAMES * ASMFX_MAX_CHANNELS * 4u)
#define ASMFX_MAP_BYTES    (ASMFX_OFF_OUT_DATA + ASMFX_RING_FRAMES * ASMFX_MAX_CHANNELS * 4u)

#pragma pack(push, 8)
struct AsmFxHeader {
    uint32_t magic;                 //   0
    uint32_t version;               //   4
    volatile uint32_t instance;     //   8  id of the effect instance that owns the rings (0: nobody)
    volatile uint32_t apo_pid;      //  12  process that hosts it (audiodg.exe)
    volatile uint32_t rate;         //  16  sample rate of the stream through the effect
    volatile uint32_t channels;     //  20
    volatile uint32_t period_frames;//  24  frames of the last call
    volatile uint32_t generation;   //  28  +1 whenever the effect restarted the rings; the engine then aligns its side
    volatile uint32_t engine_pid;   //  32  0: no engine has claimed the rings
    volatile uint32_t engine_wants; //  36  1: process the microphone, 0: pass it through untouched (nothing enabled)
    volatile uint32_t lead_frames;  //  40  how many frames the output ring should hold while processing
    volatile uint32_t apo_state;    //  44  ASMFX_STATE_*
    volatile uint64_t apo_tick_ms;  //  48  GetTickCount64() at the last call
    volatile uint64_t engine_tick_ms;// 56  GetTickCount64() when the engine last showed it is alive
    volatile uint64_t in_frames;    //  64  frames given to the engine (statistics)
    volatile uint64_t out_frames;   //  72  frames taken back
    volatile uint32_t underruns;    //  80  calls where the engine's answer was late
    volatile uint32_t overruns;     //  84  frames the input ring could not take
    volatile uint32_t calls;        //  88  number of calls
    volatile uint32_t fault;        //  92  non-zero: an exception was caught in a call (the effect then passes through)
    volatile uint32_t dll_build;    //  96  the effect's own version number
    volatile uint32_t reserved[39]; // up to 256
};
#pragma pack(pop)
static_assert(sizeof(AsmFxHeader) == 256, "AsmFxHeader must be 256 bytes");
