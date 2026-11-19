ARG NETBOX_IMAGE=docker.io/netboxcommunity/netbox:v4.7-5.1.1
FROM ${NETBOX_IMAGE}

USER root
COPY pyproject.toml README.md /plugin/
COPY netbox_windows_dhcp_plugin /plugin/netbox_windows_dhcp_plugin
# Editable install: the source directory is also bind-mounted by compose, so code edits apply on restart.
# The branching extra carries the netbox-branching and change control requirements, so they are written down once
# (pyproject.toml), as in CI.
RUN /usr/local/bin/uv pip install --python /opt/netbox/venv/bin/python -e "/plugin[branching]"
# Kerberos (lab tests against a domain, issue #62): build with KERBEROS=1. The kerberos extra builds against the
# system GSSAPI libraries, which the image does not have otherwise.
ARG KERBEROS=0
RUN if [ "$KERBEROS" = "1" ]; then \
        apt-get update && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gcc libkrb5-dev krb5-user python3-dev && \
        rm -rf /var/lib/apt/lists/* && \
        /usr/local/bin/uv pip install --python /opt/netbox/venv/bin/python -e "/plugin[branching,kerberos]"; \
    fi
