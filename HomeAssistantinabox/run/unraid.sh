#!/bin/bash

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# #  unraid.sh - Script used by HomeAssistant_inabox docker conatainer to install Home Assistant on an Unraid server        # # 
# #  by SpaceInvaderOne                                                                                                     # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
#  Get url for HA qcow2 file. Download and extract it  # # # # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 

get_vdisk() {
   

    # make the temporary directory if not there
    mkdir -p "$TMP_DIR"

    # get the html content
    if ! curl -s "$CHECKURL" -o "$TMP_FILE"; then
        echo "Error.. Failed to download the HTML page from $CHECKURL"
        return 1
    fi

    # extract the url that ends with ".qcow2.xz" to find dl url
    download_url=$(grep -oP 'href="\K.*?\.qcow2\.xz(?=")' "$TMP_FILE")

    # see if it was found
    if [[ -n "$download_url" ]]; then
        echo "Found .qcow2.xz file: $download_url"

        # set the paths for downloading and extracting
        download_path="/config/vdisk1.qcow2.xz"
        img_path="$DOMAIN/vdisk1.img"
        mkdir -p "$DOMAIN"

        # Download the needed .qcow2.xz 
        echo "downloading the vdisk to "
        if ! curl -L "$download_url" -o "$download_path"; then
            echo "Error.. Failed to download the .qcow2.xz file from $download_url"
            return 1
        fi

        # extract the .qcow2.xz file
        echo "extracting the vdisk to $download_path (removing the .xz)..."
        if ! xz -d -v "$download_path"; then
            echo "Error: Failed to extract the .qcow2.xz file"
            return 1
        fi

        # put the unzipped.qcow2 in domains location but as .vdisk1
        echo "Moving the extracted vdisk to $img_path..."
        mv "${download_path%.xz}" "$img_path"

        echo "vdisk downloaded and moved to $img_path."
    else
        echo "No .qcow2.xz file found."
        return 1
    fi

    # clean up 
    rm -f "$TMP_FILE"
}

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
#  get the IP of the runing HA VM, Restart VM, redirect to VMs WebUI # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 


get_vm_ip() {
    max_retries=5  
    retry_delay=60  
    last_vm_ip=""   
    ip_collected=false  
    ip_check_interval=3  

    while true; do
        # check the current status of the VM
        vm_status=$(virsh domstate "$VMNAME" 2>/dev/null | tr -d '\r\n')
        echo "current status of VM '$VMNAME': $vm_status"

        if [[ "$vm_status" == "running" ]]; then
            echo "VM '$VMNAME' is running."

            # has ip aleady been collected?
            if [[ "$ip_collected" == false ]]; then
                echo "Checking for the IP address of the running VM..."

                # wait until the guest agent is responsive before attempting to get the IP
                guest_agent_connected=false
                for attempt in {1..9}; do
                    # see if the qemu guest agent is connected 
                    if virsh qemu-agent-command "$VMNAME" '{"execute":"guest-ping"}' &>/dev/null; then
                        echo "Guest agent is connected."
                        guest_agent_connected=true
                        break
                    else
                        echo "Guest agent is not responding. Waiting for 5 seconds before retrying... ($attempt/9)"
                        sleep 5
                    fi
                done

                if [[ "$guest_agent_connected" == false ]]; then
                    echo "Guest agent did not respond after multiple attempts. Cannot retrieve the IP address."
                    exit 1
                fi

                # loop to check the IP address until found
                attempt=1
                while true; do
                    # get the IP address using virsh domifaddr (now that guest agent is connected)
                    vm_ip=$(virsh domifaddr "$VMNAME" --source agent | grep ipv4 | grep -v "127.0.0.1" | awk '{print $4}' | cut -d'/' -f1 | head -n 1)

                    if [[ -n "$vm_ip" ]]; then
                        echo "The IPv4 address of the VM '$VMNAME' is: $vm_ip"
                        last_vm_ip="$vm_ip"
                        ip_collected=true  # set this flag iwhen ip is successfully got

                        # update Nginx with ip of the vm so docker webui can redirct to home assistant webui in the vm
                        timestamp=$(date +%s)  
                        cat <<EOF > /etc/nginx/sites-available/default
server {
    listen 8123;
    server_name localhost;

    location / {
        # Set cache-control headers to prevent browser caching
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        add_header Pragma "no-cache";
        add_header Expires "0";

        # Clear cached redirects using the Clear-Site-Data header
        add_header Clear-Site-Data "cache";

        # Use a temporary redirect (HTTP 302) instead of permanent (HTTP 301)
        # Append a query string with a dynamic timestamp to force the browser to treat it as a new URL
        return 302 http://$vm_ip:8123?nocache=$timestamp;

        # Optionally, use an HTML refresh as a fallback
        default_type text/html;
        return 200 '<html><head><meta http-equiv="refresh" content="0;url=http://$vm_ip:8123?nocache=$timestamp"></head><body></body></html>';
    }
}
EOF

                        # reload nginx to apply the new conf
                        echo "Reloading Nginx with the new configuration..."
                        service nginx reload
                        break  # exit loop
                    else
                        echo "IP address not found yet for VM '$VMNAME'. Retrying... (Attempt $attempt)"
                        attempt=$((attempt + 1))
                        sleep "$ip_check_interval"
                    fi
                done

                if [[ -z "$vm_ip" ]]; then
                    echo "Failed to retrieve the IP address for VM '$VMNAME' after multiple attempts. Exiting..."
                    exit 1
                fi
            else
                echo "vm ip address has already been collected: $last_vm_ip"
            fi
        elif [[ "$vm_status" == "inactive" || "$vm_status" == "shut off" || "$vm_status" == "paused" ]]; then
            echo "VM '$VMNAME' is not running."

            # is restart in docker template is set to "Yes" ?
            if [[ "$RESTART" == "Yes" ]]; then
                echo "RESTART is set to 'Yes'. Attempting to start the VM..."

                retries=0
                while [[ "$retries" -lt "$max_retries" ]]; do
                    echo "Attempting to start VM '$VMNAME' (Attempt $((retries + 1))/$max_retries)..."
                    virsh start "$VMNAME"

                    # did vm start ?
                    vm_status=$(virsh domstate "$VMNAME" 2>/dev/null | tr -d '\r\n')
                    if [[ "$vm_status" == "running" ]]; then
                        echo "VM '$VMNAME' started successfully."
                        ip_collected=false  # rest the ip  flag

                    
                        break
                    else
                        echo "Failed to start VM '$VMNAME'. Retrying in $retry_delay seconds..."
                        retries=$((retries + 1))
                        sleep "$retry_delay"
                    fi
                done

                # see if loop finished but couldnt start the ha vm
                if [[ "$retries" -eq "$max_retries" ]]; then
                    echo "Failed to start VM '$VMNAME' after $max_retries attempts. Exiting..."
                    exit 1
                fi
            else
                echo "RESTART is not set to 'Yes'. Exiting script."
                exit 0
            fi
        else
            echo "uunknown  status for VM '$VMNAME': $vm_status"
        fi

        # wait before checking the ha vm status again
        if [[ "$ip_collected" == true ]]; then
            echo "Waiting for $CHECK minutes before checking again to ensure VM is still running..."
            sleep $(($CHECK * 60))  # convert mins from template into seconds
        fi
    done
}

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
#  get the host path for a given container path using Docker inspect # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 


function get_host_path {
    local container_path="$1"
    local container_id=$(hostname)

    # get container details using Docker api
    local container_details=$(curl -s --unix-socket /var/run/docker.sock http://localhost/containers/$container_id/json)

    # parse the  details & extract the corresponding host path
    local host_path=$(echo "$container_details" | jq -r --arg container_path "$container_path" '.Mounts[] | select(.Destination == $container_path) | .Source')

    # see if the host path was found
    if [ -z "$host_path" ]; then
        echo "No bind mount found for container path: $container_path"
        return 1
    else
        echo "$host_path"
    fi
}

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# Function to see the highest available qemu types on server # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 


get_highest_machine_types() {
    # q35 chipset - run using chroot and set variable
    highest_q35=$(chroot /host /usr/bin/qemu-system-x86_64 -M help | grep -o 'pc-q35-[0-9]\+\.[0-9]\+' | sort -V | tail -n 1)

    # i440fx chipset - run using chroot and set variable
    highest_i440fx=$(chroot /host /usr/bin/qemu-system-x86_64 -M help | grep -o 'pc-i440fx-[0-9]\+\.[0-9]\+' | sort -V | tail -n 1)

   # echo "Highest Q35 machine type available: $highest_q35"
   #  echo "Highest i440fx machine type available: $highest_i440fx"
}

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# Function to get the vm default vm network source # # # # # # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 

get_vm_network() {

    CONFIG_FILE="/vm/domain.cfg"

    # see if the file exists
    if [[ -f "$CONFIG_FILE" ]]; then
        # Read the BRNAME value from the file
        BRNAME=$(grep -oP '^BRNAME="\K[^"]+' "$CONFIG_FILE")

        # see if the BRNAME variable is not empty
        if [[ -n "$BRNAME" ]]; then
            # echo "Vm default network source type is set to \"$BRNAME\""
            :
        else
            echo "Vm default network source type is not set."
        fi
    else
        echo "Cant see the config file on server at /boot/config/domain.cfg"
    fi
}

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# Auto install the vm [main funcrion]  # # # # # # # # # # # # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 


autoinstall() {
    # see if the ha vm is already defined in libvirt
    if virsh dominfo "$VMNAME" &> /dev/null; then
        echo "VM '$VMNAME' is already defined. Skipping VM creation steps..."
        return 0  # exit if ha vm setup already

    # see if the required directories exist. If not, create them.
    if [ ! -d "$DOMAIN" ]; then
        mkdir -vp "$DOMAIN"
        echo "I have created the Home Assistant directories."
    else
        echo "Home Assistant directories are already present...continuing."
    fi

    # see if the ha vdisk is present. If not, download the ha vdisk from home assistant website
    if [ ! -e "$DOMAIN/vdisk1.img" ]; then
        get_vdisk
    else
        echo "There is already a vdisk image here...skipping."
        SKIPVDISK=yes
    fi

    # reset perms (prob not needed)
    chmod -R 777 "$DOMAIN/vdisk1.img"


    addxml
    definevm
}

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# ADD THE VM TEMPLATE USED TO DEFINE THE VM    # # # # # # # # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 


addxml() {

    # preparation
    
    cp "/config/HomeAssistant.xml" "/config/tmp.xml"
    XML="/config/tmp.xml"
    UUID=$(uuidgen)
    nvram_file="/var/lib/libvirt/qemu/nvram/${UUID}_VARS-pure-efi.fd"
    MAC=$(printf 'AC:87:A3:%02X:%02X:%02X\n' $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))  # random mac address with apple prefix
	

# create custom xml from the standard template

# add vm name to xml
sed -i "s#<name>.*</name>#<name>$VMNAME</name>#" "$XML"

# replace uuid with newly generated one
sed -i "s#<uuid>.*</uuid>#<uuid>$UUID</uuid>#" "$XML_FILE"

# set vm to have the highest q35 on server
sed -i "s#<type arch='x86_64' machine='XXXXXX'>#<type arch='x86_64' machine='$highest_q35'>#" "$XML"

# replace hte nvram with created nvram file
sed -i "s#<nvram>.*</nvram>#<nvram>$nvram_file</nvram>#" "$XML"

# add location for main vdisk
sed -i "s#<source file='HomeAssistant.img'/>#<source file='$DOMAINS_SHARE/$VMNAME/vdisk1.img'/>#" "$XML"

# set the MAC address
sed -i "s#<mac address='.*'/>#<mac address='$MAC'/>#" "$XML"

# set the bridge name with the value of $BRNAME
sed -i "s#<source bridge='XXX'/>#<source bridge='$BRNAME'/>#" "$XML"


# create an nvram file based off generated uuid for the vm
echo "As this is an OVMF VM, I need to create an NVRAM file. Creating now ...."
qemu-img create -f raw "$nvram_file" 64k

}



# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# Collect info and set variables # # # # # # # # # # # # # # # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 

collect_info() {
# # get the real paths of bind mapped locations in homeassistantinabox
DOMAINS_SHARE=$(get_host_path "/domains")
echo "Host path for '/domains' is $DOMAINS_SHARE"

# find out what highest q35 available is
get_highest_machine_types
echo "Highest Q35 machine type available is $highest_q35"

# find out what vm defualt network type is
get_vm_network
echo "The default VM network type is $BRNAME"

# check default CHECKURL
DEFAULT_CHECKURL="https://www.home-assistant.io/installation/linux"

# if not valid set defualt
if [[ -z "$CHECKURL" || ! "$CHECKURL" =~ ^https?:// ]]; then
    CHECKURL="$DEFAULT_CHECKURL"
    echo "Using default CHECKURL: $CHECKURL"
fi

# remp directories and paths
TMP_DIR="/config/urlscrape"
TMP_FILE="$TMP_DIR/page.html"

DOMAIN=/domains/"$VMNAME"


}


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# WAIT FOR NEEDED FILES THEN DEFINE THE VM     # # # # # # # # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 

definevm() {
    # the the vm already installed?
    if virsh dominfo "$VMNAME" &> /dev/null; then
        echo "VM '$VMNAME' is already defined. Skipping definition step..."

        # send a notification to the Unraid web GUI 
        chroot /host /usr/bin/php /usr/local/emhttp/webGui/scripts/notify \
            -e "HomeAssistantinabox" \
            -s "$VMNAME" \
            -d "VM '$VMNAME' is already defined and ready to run." \
            -i "normal"
        return 0  
    fi

    # Check supporting files are present before defining
    local xml_file="$XML"
    local qcow2_img="$DOMAIN/vdisk1.img"

    # Are all required files present?
    if [[ -f "$xml_file" && -f "$qcow2_img" ]]; then
        echo "All required files are present. Attempting to define the VM..."

        # define home assistant vm 
        if virsh define "$xml_file"; then
            rm "$xml_file"  # remove the temporary xml file 

            # send a notification to the Unraid web GUI 
            chroot /host /usr/bin/php /usr/local/emhttp/webGui/scripts/notify \
                -e "HomeAssistantinabox" \
                -s "$VMNAME" \
                -d "VM setup successfully completed. The VM is now ready to be run." \
                -i "normal"
        else
            echo "Failed to define the VM. There was an error during the VM definition process."

            # send a notification to the Unraid web GUI 
            chroot /host /usr/bin/php /usr/local/emhttp/webGui/scripts/notify \
                -e "HomeAssistantinabox" \
                -s "$VMNAME" \
                -d "VM setup failed. There was an error during the VM definition process." \
                -i "warning"

            # exit and clean up
            rm "$xml_file"
            exit 1
        fi
    else
        echo "not all required files are present. Cannot define the VM."
        echo "missing files:"
        [[ ! -f "$xml_file" ]] && echo "  - $xml_file"
        [[ ! -f "$qcow2_img" ]] && echo "  - $qcow2_img"

        # send a notification to the Unraid web GUI 
        chroot /host /usr/bin/php /usr/local/emhttp/webGui/scripts/notify \
            -e "HomeAssistantinabox" \
            -s "$VMNAME" \
            -d "Failed to setup the VM. Required files are missing." \
            -i "warning"

        exit 1
    fi
}

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 

collect_info
autoinstall
get_vm_ip &

# keep nginx running in the foreground
nginx -g "daemon off;"