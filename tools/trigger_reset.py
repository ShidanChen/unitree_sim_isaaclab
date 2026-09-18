#!/usr/bin/env python3
# trigger_reset.py
# Helper script to publish a reset command over DDS to the simulation environment.

import sys
import time
import argparse
import os
from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_

def main():
    parser = argparse.ArgumentParser(description="Trigger Simulation Environment Reset / Randomization")
    parser.add_argument(
        "--type", 
        type=str, 
        choices=["object", "all", "room-fixed-table", "ridgeback"],
        default="all", 
        help=(
            "Reset type: 'object' resets only the target, 'all' enables full "
            "randomization, and 'room-fixed-table' scrambles the room while "
            "keeping the table at its authored pose, and 'ridgeback' advances "
            "the Ridgeback to the next rear-arc position."
        ),
    )
    parser.add_argument("--domain", type=int, default=1, help="DDS Domain ID (must match ChannelFactoryInitialize(X) in sim_main.py, default is 1)")
    args = parser.parse_args()

    # Define code categories matching sim_main.py:
    # "1" = reset object / re-randomize layout
    # "2" = reset all / default restore and full randomization
    # "3" = reset room and tabletop objects while preserving the table pose
    # "4" = advance Ridgeback to the next controllable rear-arc pose
    category_code = {
        "object": "1",
        "all": "2",
        "room-fixed-table": "3",
        "ridgeback": "4",
    }[args.type]

    network_interface = os.environ.get("UNITREE_DDS_NETWORK_INTERFACE")
    print(f"Initializing DDS on domain {args.domain} (interface={network_interface or 'default'})...")
    ChannelFactoryInitialize(args.domain, networkInterface=network_interface)
    
    print("Creating publisher channel for 'rt/reset_pose/cmd'...")
    pub = ChannelPublisher("rt/reset_pose/cmd", String_)
    pub.Init()

    # Create standard string message
    msg = String_(data=category_code)

    print(f"Sending reset command: category_code={category_code} ({args.type.upper()} reset)...")
    
    # Idempotent reset commands are repeated for lossy transport. Ridgeback
    # arc control is intentionally incremental, so one user command must move
    # exactly one arc slot.
    repeat_count = 1 if args.type == "ridgeback" else 5
    for i in range(repeat_count):
        pub.Write(msg)
        time.sleep(0.1)

    print("Reset command sent successfully!")

if __name__ == "__main__":
    main()
