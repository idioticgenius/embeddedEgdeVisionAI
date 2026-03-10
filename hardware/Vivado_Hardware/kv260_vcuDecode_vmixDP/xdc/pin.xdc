# (C) Copyright 2020 - 2022 Xilinx, Inc.
# SPDX-License-Identifier: Apache-2.0

#GPIO
#Fan Speed Enable
set_property PACKAGE_PIN A12 [get_ports {fan_en_b}]
set_property IOSTANDARD LVCMOS33 [get_ports {fan_en_b}]
set_property SLEW SLOW [get_ports {fan_en_b}]
set_property DRIVE 4 [get_ports {fan_en_b}]

set_property BITSTREAM.CONFIG.OVERTEMPSHUTDOWN ENABLE [current_design]

# LED0 = Channel 0 — PMOD J2 Pin 1
set_property PACKAGE_PIN H12 [get_ports {led_4bits_tri_o[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led_4bits_tri_o[0]}]
set_property SLEW SLOW [get_ports {led_4bits_tri_o[0]}]
set_property DRIVE 4 [get_ports {led_4bits_tri_o[0]}]

# LED1 = Channel 1 — PMOD J2 Pin 2
set_property PACKAGE_PIN B10 [get_ports {led_4bits_tri_o[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led_4bits_tri_o[1]}]
set_property SLEW SLOW [get_ports {led_4bits_tri_o[1]}]
set_property DRIVE 4 [get_ports {led_4bits_tri_o[1]}]

# LED2 = Channel 2 — PMOD J2 Pin 3
set_property PACKAGE_PIN E12 [get_ports {led_4bits_tri_o[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led_4bits_tri_o[2]}]
set_property SLEW SLOW [get_ports {led_4bits_tri_o[2]}]
set_property DRIVE 4 [get_ports {led_4bits_tri_o[2]}]

# LED3 = Channel 3 — PMOD J2 Pin 4
set_property PACKAGE_PIN D11 [get_ports {led_4bits_tri_o[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led_4bits_tri_o[3]}]
set_property SLEW SLOW [get_ports {led_4bits_tri_o[3]}]
set_property DRIVE 4 [get_ports {led_4bits_tri_o[3]}]