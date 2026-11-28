# The images of the test cluster of CI, each at a tag and its digest. Nothing
# builds this file. It is here so that Renovate sees each image and moves its
# tag and digest. tests/kind/up.sh reads the references by stage name.
#
# A change here does not cut a release (the release workflow skips .github/),
# but the image job runs the tests of the PR with the new images.
#
# The OpenShell release (chart, gateway, supervisor and sandbox runtime) is not
# here: it is the anchor stage `openshell-release` of the Dockerfile of the
# image, so that one update moves the CLI in the image and the test cluster.

# The node of the kind cluster: the Kubernetes release of the test.
FROM kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5 AS kind-node

# The controller of Agent Sandbox. The test applies the release manifest of
# this version, with the image pinned to this digest.
FROM registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.12@sha256:b0ee91a42129919179baf5f429979fee3ab1a3dac6855110b4648a6ff4b44103 AS agent-sandbox-controller

# The trace collector of the trace test. It prints each span to its log.
FROM otel/opentelemetry-collector:0.139.0@sha256:8ac5df2a931e9264667b236d65bf7591fa4ba633a7a634e6caa2f0a4fc549c07 AS collector

# The image of each sandbox of the test, and of the mock model server.
FROM docker.io/library/python:3.15-slim@sha256:08a4c22c92b79ec821a7ad49768480039ee5b7accac3955228e75366e30abc8e AS python

# The update check of the host approval plugin (tests/host-approval-check).
# The central MCP gateway, at the pin of the ops repository.
FROM cr.agentgateway.dev/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b AS mcp-gateway

# The base of the fake Linux host of the check (tests/host-approval-check/sshd).
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc AS sshd-base

# The host server: the released image of tnwks-host-mcp, at its tag and
# digest (ssh-mcp v2.26.0, the pin of the ops repository).
FROM ghcr.io/it3e/tnwks-host-mcp:0.1.0@sha256:8c17e38459ed4c7bc11f967c40e73a7994a5f2bc5cac7c451a4a3f327d8909d6 AS host-mcp
