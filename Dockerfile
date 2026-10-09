# For the machine with Docker and no Python worth trusting. Built in uv's image and run in
# plain Python: the virtualenv is copied across whole, which works because both images put
# python3.13 at the same path, and keeps uv out of the image people actually run.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# --no-editable installs the package into the venv rather than linking /app/src, so the
# venv is the whole program once copied out of this stage.
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm

COPY --from=build /app/.venv /app/.venv

# Everything the tool remembers — the Evernote sync database in the working directory, the
# instance and the session under $HOME/.config/unbent — lands in /data, so one bind mount
# is the whole of the state and a second run from the same directory continues the first.
# evernote-backup reads INSIDE_DOCKER_CONTAINER to know it cannot open a browser itself.
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/data \
    INSIDE_DOCKER_CONTAINER=1
WORKDIR /data
VOLUME /data

ENTRYPOINT ["evernote2unbent"]
CMD ["--help"]
