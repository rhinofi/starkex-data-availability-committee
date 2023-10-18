#!/usr/bin/env bash
set -ueo pipefail

batch_id=$1

/app/build/Release/src/starkware/committee/dump_trees \
  --config_file=/config.yml \
  --vaults_file=/data/vaults.csv \
  --nodes_file=/data/nodes.csv \
  --batch_id=$batch_id vaults
