#!/bin/bash

# Start SSH daemon
/usr/sbin/sshd

# Start iperf3 server in background (optional, can be started manually)
# iperf3 -s -D

# Keep container running
exec tail -f /dev/null
