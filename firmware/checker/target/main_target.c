/* Bare-metal test image for the emulator (Renode, STM32F4 platform).
 * Reads G-code text from 0x90000000 (u32 length + bytes), runs gcheck on it
 * line by line, stores the result in `gc_out` and calls gc_done() - the
 * emulator hooks that address, reads the result, the instruction count and
 * the stack high-water mark, and stops. A fault calls gc_fault() instead.
 */
#include <stdint.h>
#include <string.h>

#include "../gcheck.h"

#ifndef STRIDE
#define STRIDE 1
#endif

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack, _stack_bottom;
int main(void);

/* the state block, full capacities (checked at start: gc_init fails if it does not fit) */
static uint64_t S_mem[57 * 1024 / 8];
volatile gc_result_t gc_out;
volatile uint32_t gc_stack_used, gc_state_size, gc_status;
/* flat report for the emulator: status, stack used, state bytes, block, n_evid, lines, layers,
   sampled, loops, circles, elements, max_circ, max_track, overflow, v_cyl, h_cyl, slabs, pairs,
   max_run, max_ptrack, then (type, ref) per evidence */
volatile uint32_t gc_report[32];

__attribute__((noinline, used)) void gc_done(void) { __asm volatile("nop"); for (;;) {} }
__attribute__((noinline, used)) void gc_fault(void) { __asm volatile("nop"); for (;;) {} }

#define STACK_PAINT 0xC0DEF00Du

void Reset_Handler(void)
{
    uint32_t *src = &_sidata, *dst = &_sdata;
    while (dst < &_edata) *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss; dst++) *dst = 0;
    /* paint the stack below the current frame */
    uint32_t sp;
    __asm volatile("mov %0, sp" : "=r"(sp));
    for (dst = &_stack_bottom; (uint32_t)dst < sp - 64; dst++) *dst = STACK_PAINT;
    /* enable the FPU (CP10, CP11 full access) */
    *(volatile uint32_t *)0xE000ED88 |= (0xFu << 20);
    __asm volatile("dsb; isb");
    main();
    gc_done();
}

void Fault_Handler(void) { gc_status = 0xFA17u; gc_report[0] = 0xFA17u; gc_fault(); }

__attribute__((section(".isr_vector"), used)) const void *vectors[16] = {
    (void *)&_estack, (void *)Reset_Handler, (void *)Fault_Handler, (void *)Fault_Handler,
    (void *)Fault_Handler, (void *)Fault_Handler, (void *)Fault_Handler, 0, 0, 0, 0,
    (void *)Fault_Handler, (void *)Fault_Handler, 0, (void *)Fault_Handler, (void *)Fault_Handler};

int main(void)
{
    const uint8_t *base = (const uint8_t *)0x90000000u;
    uint32_t len = *(const uint32_t *)base;
    const char *p = (const char *)base + 4, *end = p + len;
    gc_state_t *S = gc_init(S_mem, sizeof S_mem, &gc_caps_full, STRIDE);
    if (!S) gc_fault();
    while (p < end) {
        const char *q = p;
        while (q < end && *q != '\n') q++;
        int n = (int)(q - p);
        if (n && p[n - 1] == '\r') n--;
        gc_line(S, p, n);
        p = q + 1;
    }
    gc_finish(S);
    memcpy((void *)&gc_out, &S->res, sizeof S->res);
    const uint32_t *w = &_stack_bottom;
    while (*w == STACK_PAINT) w++;
    gc_stack_used = (uint32_t)&_estack - (uint32_t)w;
    gc_state_size = gc_state_bytes(&gc_caps_full);
    gc_status = 0x600Du;
    const gc_result_t *r = &S->res;
    uint32_t v[24] = {gc_status, gc_stack_used, gc_state_size, (uint32_t)r->block, (uint32_t)r->n_evid, r->lines,
                      r->layers, r->sampled, r->loops, r->circles, r->elements, r->max_circ, r->max_track, r->overflow,
                      r->v_cyl, r->h_cyl, r->slabs, r->pairs, r->max_run, r->max_ptrack,
                      r->t_cyl, r->t_slabs, r->ellipses, r->max_etrack};
    for (int i = 0; i < 24; i++) gc_report[i] = v[i];
    for (int i = 0; i < r->n_evid && 24 + 2 * i + 1 < 32; i++) {
        gc_report[24 + 2 * i] = r->evid[i].type;
        gc_report[25 + 2 * i] = (uint32_t)(int32_t)r->evid[i].ref;
    }
    return 0;
}
