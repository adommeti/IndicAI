ARG SERVICE_IMAGE=busybox:1.37
FROM golang:1.24-alpine AS probe
COPY infra/docker/healthcheck.go /src/healthcheck.go
RUN CGO_ENABLED=0 go build -o /healthcheck /src/healthcheck.go
FROM ${SERVICE_IMAGE}
COPY --from=probe /healthcheck /usr/local/bin/healthcheck
