dev-up-build-detach:
	$(MAKE) dev-base-build # $(MAKE) command calls on this Makefile, meaning it will call the command inside this Makefile (recursive calls)
	$(MAKE) dev-build args=-d
	$(MAKE) dev-migrate
	$(MAKE) dev-collect-static

dev-up-build:
	$(MAKE) dev-base-build
	$(MAKE) dev-build

dev-post-build:
	$(MAKE) dev-migrate
	$(MAKE) dev-collect-static

dev-base-build:
	@echo "\n\n\nBuilding base image.."
	docker build -f docker_management/backend/dev.base.Dockerfile -t beiwe-server-dev-base .

dev-build:
	@echo "\n\n\nBuilding images and running containers.."
	docker compose -f docker_management/dev.docker-compose.yml --env-file docker_management/.envs/.env.dev up --build $(args)

dev-migrate:
	@echo "\n\n\nMigrating database.."
	docker compose -f docker_management/dev.docker-compose.yml --env-file docker_management/.envs/.env.dev exec -u 0 web python manage.py migrate --noinput

dev-collect-static:
	@echo "\n\n\nCollecting static files.."
	docker compose -f docker_management/dev.docker-compose.yml --env-file docker_management/.envs/.env.dev exec -u 0 web python manage.py collectstatic --no-input --clear

prod-up-build-detach:
	$(MAKE) prod-base-build
	$(MAKE) prod-build args=-d
	$(MAKE) prod-migrate
	$(MAKE) prod-collect-static

prod-up-build:
	$(MAKE) prod-base-build
	$(MAKE) prod-build

prod-post-build:
	$(MAKE) prod-migrate
	$(MAKE) prod-collect-static

prod-base-build:
	@echo "\n\n\nBuilding base image.."
	docker build -f docker_management/backend/prod.base.Dockerfile -t beiwe-server-prod-base .

prod-build:
	@echo "\n\n\nBuilding images and running containers.."
	docker compose -f docker_management/prod.docker-compose.yml --env-file docker_management/.envs/.env.prod up --build $(args)

prod-migrate:
	@echo "\n\n\nMigrating database.."
	docker compose -f docker_management/prod.docker-compose.yml --env-file docker_management/.envs/.env.prod exec -u 0 web python manage.py migrate --noinput

prod-collect-static:
	@echo "\n\n\nCollecting static files.."
	docker compose -f docker_management/prod.docker-compose.yml --env-file docker_management/.envs/.env.prod exec -u 0 web python manage.py collectstatic --no-input --clear

# --- Upload Metadata Index dashboard deploy helpers ---------------------------
# Wraps cluster_management/cdk/deploy_metadata_index.sh. Mutating targets are
# PREVIEW-ONLY by default; pass APPLY=true to actually change infrastructure
# (EXECUTE=true for the destructive reset). Override AWS_PROFILE / AWS_REGION /
# READER_PRINCIPAL_ARN as needed, e.g.:
#   make metadata-index-deploy AWS_PROFILE=eb-cli            # preview
#   make metadata-index-deploy AWS_PROFILE=eb-cli APPLY=true # deploy + set web env
METADATA_INDEX_SCRIPT := cluster_management/cdk/deploy_metadata_index.sh
export AWS_PROFILE AWS_REGION READER_PRINCIPAL_ARN EB_APP EB_ENV STACK_NAME I_UNDERSTAND_THIS_DELETES_DATA

.PHONY: metadata-index-web-arn metadata-index-outputs metadata-index-set-env metadata-index-deploy metadata-index-backfill metadata-index-reset

metadata-index-web-arn:
	$(METADATA_INDEX_SCRIPT) web-arn

metadata-index-outputs:
	$(METADATA_INDEX_SCRIPT) outputs

metadata-index-set-env:
	$(METADATA_INDEX_SCRIPT) set-env $(if $(filter true,$(APPLY)),--apply,)

metadata-index-deploy:
	$(METADATA_INDEX_SCRIPT) deploy $(if $(filter true,$(APPLY)),--apply,)

metadata-index-backfill:
	$(METADATA_INDEX_SCRIPT) backfill $(if $(filter true,$(APPLY)),--apply,)

metadata-index-reset:
	$(METADATA_INDEX_SCRIPT) reset $(if $(filter true,$(EXECUTE)),--execute,)