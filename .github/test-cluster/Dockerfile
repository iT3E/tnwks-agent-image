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
FROM registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.0.10@sha256:594f4853913bca2b336d0267fe0978445f1aaa5837ed23797e48e745c8d6b610 AS agent-sandbox-controller

# The trace collector of the trace test. It prints each span to its log.
FROM otel/opentelemetry-collector:0.139.0@sha256:8ac5df2a931e9264667b236d65bf7591fa4ba633a7a634e6caa2f0a4fc549c07 AS collector

# The image of each sandbox of the test, and of the mock model server.
FROM docker.io/library/python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS python
