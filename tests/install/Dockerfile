# A bare Ubuntu with nothing preinstalled — the closest honest stand-in
# for a fresh WSL2 distro, which is exactly this plus Docker Desktop's
# socket. Used by tests/install/run.sh to prove the documented install
# works on a machine that has never seen this project.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# python3 + git: stated requirements. docker.io: the app uses the SDK but
# also shells out to the `docker` binary, so the CLI must be on PATH.
# curl/ca-certificates: fetching uv, and smoke-testing the HTTP routes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv git docker.io curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The README's primary install route.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

COPY check.sh /check.sh
RUN chmod +x /check.sh
ENTRYPOINT ["/check.sh"]
