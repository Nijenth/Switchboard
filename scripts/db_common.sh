#!/usr/bin/env bash

db_psql() {
    PGPASSWORD="${SWITCHBOARD_DB_OWNER_PASSWORD:?Missing owner password}" \
        psql --no-psqlrc --no-password --set=ON_ERROR_STOP=1 \
        --host="${SWITCHBOARD_DB_SOCKET:?Run through scripts/check_db}" \
        --port=5432 --username=switchboard_owner --dbname=switchboard_test "$@"
}

apply_db_sql() {
    local directory=$1
    local LC_ALL=C
    local file name version previous_version=''
    local -a files=("$directory"/*.sql)

    for file in "${files[@]}"; do
        name=${file##*/}
        if [[ ! -f $file || ! $name =~ ^([0-9]{3})_.+\.sql$ ]]; then
            printf 'Expected a numbered SQL file in %s, got %s\n' "$directory" "$name" >&2
            return 1
        fi
        version=${BASH_REMATCH[1]}
        if [[ $version == "$previous_version" ]]; then
            printf 'Duplicate SQL version %s in %s\n' "$version" "$directory" >&2
            return 1
        fi
        previous_version=$version
    done

    for file in "${files[@]}"; do
        printf 'Applying %s\n' "$file"
        db_psql --file="$file" || return "$?"
    done
}
