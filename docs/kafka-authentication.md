# Kafka TLS and authentication

The API listener reads `KAFKA_CLIENT_CONFIG_FILE`, a private JSON file with
librdkafka-style TLS/SASL property names. It maps these to kafka-python settings
for both producer and consumer. Set the listener's existing
`kafka.bootstrap_servers` configuration to the authenticated endpoint.

Required SASL_SSL fields are `security.protocol`, `sasl.mechanism`, `sasl.username`,
`sasl.password`, and `ssl.ca.location`. Hostname verification stays enabled.
The loader rejects unrelated consumer/offset settings. Keep this file outside
version control with access restricted to the service user.
