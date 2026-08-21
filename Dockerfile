FROM ubuntu:24.04

# libvirt-clients provides virsh, qemu-utils provides qemu-img, and php-cli runs
# Unraid's own notify script when it is mounted in. curl is what most Unraid
# notification agent scripts call.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        libvirt-clients \
        qemu-utils \
        php-cli \
        curl \
        ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# The apprise binary some Unraid notification agents call. Pinned to its sha256.
RUN curl -fsSL -o /usr/bin/apprise \
        https://github.com/unraid/apprise-go/releases/download/v0.2.8/apprise-go-linux-amd64 && \
    echo "6b7c01eafdac94f95623f6e61f6dd40d50698744feafb47416ec49827b62326a  /usr/bin/apprise" \
        | sha256sum -c - && \
    chmod 0755 /usr/bin/apprise

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY app/ /app/app/

# Ubuntu marks its system python as externally managed (PEP 668). This is a
# single-purpose image with no other Python tooling to isolate from, so install
# directly rather than adding a venv.
RUN pip install --break-system-packages --no-cache-dir /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The setup wizard. Home Assistant's own port is left alone.
EXPOSE 9123

# The wizard is the default. CLI subcommands are reachable by overriding CMD.
ENTRYPOINT ["python3", "-m", "app"]
CMD ["serve"]
