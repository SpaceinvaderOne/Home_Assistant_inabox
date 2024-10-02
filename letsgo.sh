#!/bin/bash

start() {
    if [ -f /config/delete_me_to_reset_v2.txt ]; then
        chmod -R 777 /config/
        cd /config/run
        ./unraid.sh  
    else
        rm -f /config/*.xml
        rm -f /config/*.txt
        rm -f /config/Hassio_2.png
        rm -rf /config/run
        rm -rf /config/urlscrape
        cp -r /app/HomeAssistantinabox/* /config/
        chmod -R 777 /config/
        cd /config/run
        ./unraid.sh  
    fi
}

start

sleep "$SLEEPTIME"
