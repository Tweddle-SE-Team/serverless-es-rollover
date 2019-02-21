from __future__ import print_function
import os
import certifi
import curator
import json
import time
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
    newIndex = datetime.now().strftime("%Y%m%d")
    for alias in getAllAliases(es):
        curator.Rollover(es, alias, conditions, new_index=newIndex)


def getRoleARN(region):
    sts = boto3.client('sts', region)
    return "/".join(sts.get_caller_identity()['Arn']
                    .replace(":sts:", ":iam:")
                    .replace("assumed-role", "role")
                    .split("/")[:-1])


def createRepository(es, repository, bucket, region=None):
    if not region:
        region = os.getenv('AWS_DEFAULT_REGION')
    try:
        es.snapshot.get_repository(repository=repository)
    except:
        es.snapshot.create_repository(
            repository=repository,
            body={
                "type": "s3",
                "settings": {
                    "region": region,
                    "bucket": bucket,
                    "role_arn": getRoleARN(region)
                }
            },
            request_timeout=30,
            verify=True)


def createSnapshots(es, repository):
    nonAliasedIndices = curator.IndexList(es)
    aliases = getAllAliases(es)
    nonAliasedIndices.filter_by_alias(aliases=aliases, exclude=True)
    nonAliasedIndices.filter_by_regex(kind="prefix", value=".monitoring-", exclude=True)
    for index in nonAliasedIndices.indices:
        try:
            snapshot = client.snapshot.get(repository=repository, snapshot=index)
            if snapshot["state"] == "FAILED":
                #notify
        except:
            es.snapshot.create(
                repository=repository,
                snapshot=index,
                wait_for_completion=False)


def deleteIndices(es, keep):
    aliases = getAllAliases(es)
    for alias in aliases:
        indices = curator.IndexList(es)
        pattern = "^%s-(.*)" % (alias)
        indices.filter_by_count(
            count=keep,
            reverse=True,
            use_age=True,
            pattern=pattern,
            source='creation_date')
        curator.DeleteIndices(indices)


def handler(event, context):
    configJson = os.getenv("SERVERLESS_CONFIG_JSON")
    if not configJson:
        raise ConfigNotFoundException("ES clusters cnfiguration was not defined. Please use SERVERLESS_CONFIG_JSON variable to set it")
    else:
        config = json.loads(configJson)
        bucket = config["bucket"]
    for cluster in config["clusters"]:
        clusterConfig = config[cluster]
        address = clusterConfig["address"]
        es = getESClient(address)
        rolloverCluster(es, clusterConfig["rollover_conditions"])
        createRepository(es, repository=cluster, bucket=bucket)
        createSnapshots(es, repository=cluster)
        deleteIndices(es, keep=clusterConfig["indices_to_keep"])
