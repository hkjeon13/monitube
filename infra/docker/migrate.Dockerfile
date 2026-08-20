# Migration runner.
#
# Compose bind-mounts database/migrations and scripts/apply_migrations.sh into a
# stock postgres image. Kubernetes has no host to bind-mount from, so the same
# files are baked into an image instead. Devtron builds this like any other
# service image.
FROM postgres:16-alpine

COPY database/migrations /migrations
COPY scripts/apply_migrations.sh /usr/local/bin/apply_migrations.sh

RUN chmod 0555 /usr/local/bin/apply_migrations.sh

# Never run migrations as root.
USER postgres

ENTRYPOINT ["/bin/sh", "/usr/local/bin/apply_migrations.sh"]
