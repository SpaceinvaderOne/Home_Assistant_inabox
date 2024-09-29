# HomeAssistant_inabox

**HomeAssistant_inabox** is a powerful, easy-to-use Docker container designed to seamlessly download and install a fully functional Home Assistant VM onto an Unraid server. It simplifies the installation and management of Home Assistant by integrating a virtual machine (VM) setup directly from the official Home Assistant source.

## Key Features

- **Direct Download & Installation**: Automatically downloads the Home Assistant `.qcow2` image from the official Home Assistant release repository and installs it onto your Unraid server.
- **Automated VM Setup**: Handles the creation of a new VM configuration, dynamically building the XML template based on your environment and the highest QEMU version available.
- **Automatic VM Monitoring**: Regularly checks the status of the VM and restarts it if it has been shut down unexpectedly, ensuring Home Assistant is always available.
- **Seamless Integration with Docker & VM WebUI**: Combines Docker container management with VM monitoring. Clicking the “WebUI” link from the Unraid Docker tab will automatically redirect to the Home Assistant WebUI inside the VM.
- **Dynamic IP Management**: Automatically determines the internal IP address of the Home Assistant VM and updates the Docker WebUI redirect, so you always have access even if the VM’s IP address changes.

---

## Getting Started

Follow these instructions to install and configure the HomeAssistant_inabox container on your Unraid server.

### Installation

1. **Go to the Unraid Apps Tab** (also known as **CA Community Applications**).
2. **Search for `HomeAssistant_inabox`** and click **Install**.
3. **Configure the container variables** as described below in the “Configuration” section.
4. **Start the container.** HomeAssistant_inabox will automatically download the latest Home Assistant image, create a VM, and set up the necessary configuration files.
5. Once the setup is complete, **click the container’s “WebUI” button** to access your Home Assistant instance.

---

## Configuration

HomeAssistant_inabox relies on a few essential variables that need to be set through the Unraid Docker template. Below is a detailed description of each option and its purpose:

### Container Variables

1. **`VMNAME`**  
   - **Description:** Set the name of the Home Assistant VM.  
   - **Default:** `Home Assistant`  
   - **Purpose:** This is the VM name that will be displayed in Unraid’s VM manager.

2. **`VM Images Location`**  
   - **Description:** Specify the location where the VM images are stored (e.g., your Domains share).  
   - **Example:** `/mnt/user/domains/`  
   - **Purpose:** Defines the storage path for the Home Assistant VM files on your Unraid server.

3. **`Appdata Location`**  
   - **Description:** Set the path where HomeAssistant_inabox stores its appdata and configuration files.  
   - **Default:** `/mnt/user/appdata/homeassistantinabox/`  
   - **Purpose:** Specifies where the container’s internal configuration and scripts are stored.

4. **`Keep VM Running`**  
   - **Description:** If set to `Yes`, the container will automatically monitor the Home Assistant VM and restart it if it’s not running.  
   - **Default:** `Yes`  
   - **Purpose:** Ensures that Home Assistant remains available, even after unexpected shutdowns.

5. **`Check Time`**  
   - **Description:** Defines the frequency (in minutes) for checking the status of the Home Assistant VM.  
   - **Default:** `15`  
   - **Purpose:** Determines how often the container checks to see if the VM is running and needs to be started.

6. **`WEBUI_PORT`**  
   - **Description:** Set the port for accessing the Home Assistant WebUI through the container’s WebUI redirect.  
   - **Default:** `8123`  
   - **Purpose:** Allows you to configure the WebUI access port for Home Assistant.

---

## How It Works

HomeAssistant_inabox provides a robust solution by combining a Docker container with a full VM environment:

1. **Direct Download & Installation**:  
   - When the container is started, it automatically downloads the latest Home Assistant `.qcow2` disk image from the official Home Assistant source.  
   - It then extracts and moves the image to your Unraid server’s specified domains location.

2. **Dynamic VM Setup**:  
   - The container dynamically builds a VM XML template for Home Assistant using the latest QEMU version available.  
   - This template is then used to define a new VM on your Unraid server.

3. **Automatic IP Detection**:  
   - After the VM is started, the container uses the QEMU guest agent to retrieve the internal IP address of the VM.  
   - The IP address is then used to configure a redirect within the Docker container, making the “WebUI” link in Unraid’s Docker tab point directly to the Home Assistant WebUI inside the VM.

4. **Monitoring & Restart Functionality**:  
   - If `RESTART` is set to `Yes`, the container will regularly check to see if the Home Assistant VM is running.  
   - If the VM is found to be shut down or paused, the container will attempt to start it automatically.

5. **WebUI Integration**:  
   - When you click the WebUI button for the HomeAssistant_inabox container, it dynamically redirects to the Home Assistant WebUI inside the VM using the IP address retrieved during the last check.

---

## Example Use Case

1. **Scenario**: You want to run Home Assistant on your Unraid server, but you also want to manage it through the Docker interface rather than a separate VM management tab then having to use browser to login. 
2. **Solution**: Install HomeAssistant_inabox and configure the necessary variables. With this setup, Home Assistant will run as a VM but be accessible directly from the Docker WebUI link.  
3. **Result**: This simplifies your environment and provides a single point of access for Home Assistant, combining the convenience of Docker management with the power and flexibility of a dedicated VM.

---

## Contributions & Support

Feel free to contribute to the development of HomeAssistant_inabox by submitting pull requests or opening issues . For support and questions, join the Unraid community forum thread for HomeAssistant_inabox.

### License

This project is licensed under the MIT License. See the `LICENSE` file for more details.

---

## Acknowledgements

Special thanks to the Unraid community for their support and contributions, as well as the Home Assistant team for providing a powerful home automation platform.
