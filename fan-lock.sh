#!/bin/bash
# fan-lock.sh: keeps the board's fan pinned to a fixed cooling state instead
# of letting the kernel's thermal governor ramp it up/down. Switches the
# thermal zone to the user_space governor once, then re-asserts the target
# cooling state on an interval as a defensive measure in case anything else
# resets it.
#
# Copyright (c) 2026 Jason Ardon (W3ndees)
# Licensed under the MIT License - see LICENSE in this repository.
echo user_space > /sys/class/thermal/thermal_zone0/policy

while true; do
    echo 2 > /sys/class/thermal/cooling_device0/cur_state
    sleep 10
done
