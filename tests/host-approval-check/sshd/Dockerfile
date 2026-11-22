# The fake Linux host of the update check: OpenSSH with one normal user,
# `agent`, key sign-in only. No key is in the image: the run makes new keys
# each time and mounts them; the start step installs them.
# The base is the `sshd-base` stage of .github/test-cluster/Dockerfile,
# where Renovate moves it (Renovate does not read files under tests/).
ARG BASE
FROM ${BASE}
RUN apk add --no-cache openssh-server bash coreutils procps \
 && adduser -D -s /bin/bash agent \
 && passwd -u agent >/dev/null 2>&1 || true \
 && install -d -o agent -g agent -m 700 /home/agent/.ssh \
 && echo "root only" > /etc/lab-root-only && chmod 600 /etc/lab-root-only \
 && echo "lab host ok" > /etc/lab-motd && chmod 644 /etc/lab-motd \
 && touch /home/agent/.bashrc /home/agent/.profile \
 && chown root:root /home/agent/.bashrc /home/agent/.profile && chmod 644 /home/agent/.bashrc /home/agent/.profile
COPY sshd_config /etc/ssh/sshd_config
EXPOSE 22
CMD ["sh", "-c", "install -o agent -g agent -m 600 /k/agent.pub /home/agent/.ssh/authorized_keys && install -m 600 /k/host /etc/ssh/ssh_host_ed25519_key && exec /usr/sbin/sshd -D -e"]
