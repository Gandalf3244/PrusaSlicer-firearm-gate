/* STM32F427ZI as on the Prusa xBuddy: 2 MB flash, 192 KB SRAM (+64 KB CCM, unused here).
   The G-code under test is placed by the emulator at 0x90000000 (stands in for the USB drive):
   a 32-bit length followed by the text. */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 2048K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 192K
}
_estack = ORIGIN(RAM) + LENGTH(RAM);
_stack_size = 16K;                 /* painted at reset; the high-water mark is measured */

ENTRY(Reset_Handler)
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) *(.glue_7) *(.glue_7t) KEEP(*(.init)) KEEP(*(.fini)) . = ALIGN(4); _etext = .; } > FLASH
  .ARM.exidx : { *(.ARM.exidx*) } > FLASH
  _sidata = LOADADDR(.data);
  .data : { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM AT > FLASH
  .bss (NOLOAD) : { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
  _stack_bottom = _estack - _stack_size;
  ASSERT(_ebss <= _stack_bottom, "RAM overflow: state + stack do not fit")
}
