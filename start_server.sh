#!/bin/bash
cd /root/srs-project/srs
export USE_GEVENT=false
nohup python3 callback_server.py > callback_server.log 2>&1 &
echo "Started callback_server, PID: $!"
sleep 2
curl -s http://localhost:8085/health && echo " - Service OK" || echo " - Service may need more time"
