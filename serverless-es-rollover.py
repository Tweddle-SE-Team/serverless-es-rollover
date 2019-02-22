from __future__ import print_function
import boto3
import os
import certifi
import curator
import json
import time
import botocore.session
from curator.exceptions import NoIndices
from elasticsearch import Elasticsearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
from datetime import datetime


class ConfigNotFoundException(Exception):
    pass


def getESClient(address, region=None):
    service = 'es'
    if not region:
        region = os.getenv('AWS_DEFAULT_REGION')
    credentials = boto3.Session().get_credentials()
    awsAuth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        region,
        service,
        session_token=credentials.token)
    return Elasticsearch(address, http_auth=awsAuth, connection_class=RequestsHttpConnection)


def getAllAliases(es):
    catAliases = es.cat.aliases(format="json")
    return [alias['alias'] for alias in catAliases]


def rolloverCluster(es, conditions):
    suffix = datetime.now().strftime("%Y%m%d")
    for alias in getAllAliases(es):
        newIndex = "%s-%s" % (alias, suffix)
        rolloverIndices = curator.Rollover(es, alias, conditions, new_index=newIndex)
        rolloverIndices.do_action()



def credentials(region):
    sts = boto3.client('sts', region)
    arn = sts.get_caller_identity()['Arn']
    if "sts" in arn:
        return {"role_arn": "/".join(sts.get_caller_identity()['Arn']
                                    .replace(":sts:", ":iam:")
                                    .replace("assumed-role", "role")
                                    .split("/")[:-1]),
                "region": region}
    else:
        session = botocore.session.get_session()
        return {
            "endpoint": "http://10.5.0.6:9000",
            "protocol": "http"}


def getAllRepositories(es):
    catRepositories = es.cat.repositories(format="json")
    return [repo['id'] for repo in catRepositories]


def getAllSnapshots(es, repository):
    catSnapshots = es.cat.snapshots(repository=repository, format="json")
    return [snapshot['id'] for snapshot in catSnapshots]


def createRepository(es, repository, bucket, region=None):
    if not region:
        region = os.getenv('AWS_DEFAULT_REGION')
    if repository not in getAllRepositories(es):
        es.snapshot.create_repository(
            repository=repository,
            body={
                "type": "s3",
                "settings": {
                    "bucket": bucket,
                    **credentials(region)
                }
            },
            request_timeout=30,
            verify=True)


def createSnapshots(es, repository):
    nonAliasedIndices = curator.IndexList(es)
    aliases = getAllAliases(es)
    if aliases:
        nonAliasedIndices.filter_by_alias(aliases=aliases, exclude=True)
        nonAliasedIndices.filter_by_regex(kind="prefix", value=".monitoring-", exclude=True)
        for index in nonAliasedIndices.indices:
            if repository in getAllRepositories(es):
                if index in getAllSnapshots(es, repository):
                    snapshots = es.snapshot.get(repository=repository, snapshot=index)
                    for snapshot in snapshots["snapshots"]:
                        if snapshot["state"] == "FAILED":
                            pass
                            #notify
                else:
                    es.snapshot.create(
                        repository=repository,
                        snapshot=index,
                        body={
                            "indices": index,
                            "include_global_state": False
                        },
                        wait_for_completion=False)
    else:
        pass
        # no aliases found


def deleteIndices(es, keep, repository):
    indices = curator.IndexList(es)
    snapshots = indices.indices
    indices.filter_by_count(
        count=keep,
        reverse=True,
        pattern="^(.*)-\d{8}.*$")
    for snap in snapshots:
        if snap in getAllSnapshots(es, repository):
            snapshot = es.snapshot.get(repository=repository, snapshot=snap)["snapshots"][0]
            if snapshot["state"] != "SUCCESS":
                indices.filter_by_regex(kind="prefix", value=snap, exclude=True)
        else:
            indices.filter_by_regex(kind="prefix", value=snap, exclude=True)
    deleteIndices = curator.DeleteIndices(indices)
    deleteIndices.do_action()



def handler(event, context):
    configJson = os.getenv("SERVERLESS_CONFIG_JSON")
    if not configJson:
        raise ConfigNotFoundException("ES clusters cnfiguration was not defined. Please use SERVERLESS_CONFIG_JSON variable to set it")
    else:
        config = json.loads(configJson)
        bucket = config["bucket"]
        clusters = config["clusters"]
    for cluster in clusters:
        clusterConfig = clusters[cluster]
        address = clusterConfig["address"]
        es = getESClient(address)
        rolloverCluster(es, clusterConfig["rollover_conditions"])
        createRepository(es, repository=cluster, bucket=bucket)
        createSnapshots(es, repository=cluster)
        deleteIndices(es, repository=cluster, keep=clusterConfig["indices_to_keep"])


if __name__ == "__main__":
    handler(None, None)
