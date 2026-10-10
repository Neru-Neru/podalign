.PHONY: start stop clean-data

start:
	docker compose up --build

stop:
	docker compose down

clean-data: stop
	docker compose run --rm --no-deps --entrypoint sh podalign -c 'find /data -mindepth 1 -delete'
	docker compose down
	rmdir ./data
