# The air-gap rehearsal harness (Phase 8 gate). Never shipped, never deployed.
#
# `make gate-phase8` runs offline/install.sh inside this image with `--network none` and
# the Docker socket bind-mounted. That is the only honest way to prove the claim the
# bundle makes: a container with no network at all can still install the whole platform,
# because a unix socket is not a network. Anything the installer quietly fetches fails
# here, on a build machine, instead of on a closed network with no way to investigate.
#
# Built by COPY from two digest-pinned images rather than by installing packages: Rule 5
# forbids OS package installs at image build, and there is no reason for this image to be
# the one exception. The docker CLI is a static binary; taking it from the official image
# is both simpler and more reproducible than an apt repository.
FROM docker@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d AS cli

# The same interpreter the target is assumed to have (bash and python3, nothing else).
FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=cli /usr/local/libexec/docker/cli-plugins/docker-compose \
                /usr/local/libexec/docker/cli-plugins/docker-compose

ENTRYPOINT ["/bin/bash"]
