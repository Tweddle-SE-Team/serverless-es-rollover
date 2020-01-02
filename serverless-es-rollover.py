import boto3
import os
import curator
import json
import logging
import botocore.session
from logging.config import fileConfig
from curator.exceptions import NoIndices
from elasticsearch import Elasticsearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
from datetime import datetime


fileConfig('logging.ini')
logger = logging.getLogger()


class ConfigNotFoundException(Exception):
    pass


def getESClient(address, region=None):
    service = 'es'
    if not region:
        logger.info("Region is not defined. Getting default region environment variable")
        region = os.getenv('AWS_DEFAULT_REGION')
    credentials = boto3.Session().get_credentials()
    awsAuth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        region,
        service,
        session_token=credentials.token)
    return Elasticsearch(address, http_auth=awsAuth, connection_class=RequestsHttpConnection)


def getAllAliases(es, exclude=[]):
    allAliases = es.cat.aliases(format="json")
    for alias in allAliases:
        if not alias['alias'] in exclude:
            yield alias['alias']


def rolloverCluster(es, conditions, excludeAliases):
    suffix = datetime.now().strftime("%Y%m%d")
    for alias in getAllAliases(es, excludeAliases):
        newIndex = "%s-%s" % (alias, suffix)
        if newIndex in curator.IndexList(es).indices:
            logger.error("Index with name %s already exists. Check you rollover conditions or update naming" % (newIndex))
        else:
            logger.info("Performing rollover of %s to a new index %s" % (alias, newIndex))
            rolloverIndices = curator.Rollover(es, alias, conditions, new_index=newIndex)
            rolloverIndices.do_action()
            logger.info("Rollover of %s succeeded" % (alias))



def credentials(region):
    sts = boto3.client('sts', region)
    arn = sts.get_caller_identity()['Arn']
    if "sts" in arn:
        roleARN = "/".join(sts.get_caller_identity()['Arn']
                            .replace(":sts:", ":iam:")
                            .replace("assumed-role", "role")
                            .split("/")[:-1])
        logger.info("Got AWS Role ARN for backups: %s" % (roleARN))
        return {
            "role_arn": roleARN,
            "region": region}
    else:
        session = botocore.session.get_session()
        logger.info("Generating configurations for a local setup")
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
                    "base_path": repository,
                    **credentials(region)
                }
            },
            request_timeout=30,
            verify=True)
    else:
        logger.info("Repository %s already exists" % (repository))


def createSnapshots(es, repository, excludeAliases):
    nonAliasedIndices = curator.IndexList(es)
    aliases = [getAllAliases(es, excludeAliases)]
    if aliases:
        nonAliasedIndices.filter_by_alias(aliases=aliases, exclude=True)
        if nonAliasedIndices.indices:
            nonAliasedIndices.filter_by_regex(kind="prefix", value=".monitoring-", exclude=True)
            for index in nonAliasedIndices.indices:
                if repository in getAllRepositories(es):
                    if index in getAllSnapshots(es, repository):
                        logger.info("Found %s snapshot" % (index))
                        snapshots = es.snapshot.get(repository=repository, snapshot=index)
                        for snapshot in snapshots["snapshots"]:
                            if snapshot["state"] == "FAILED":
                                logger.info("Snapshot %s is in a failed state" % (index))
                            else:
                                logger.debug("Snapshot %s is in %s state" % (index, snapshot["state"]))
                    else:
                        es.snapshot.create(
                            repository=repository,
                            snapshot=index,
                            body={
                                "indices": index,
                                "include_global_state": False
                            },
                            wait_for_completion=False)
                        logger.info("Created %s snapshot" % (index))
                else:
                    logger.error("Repository %s is not found" % (repository))
        else:
            logger.info("No non-aliased indices found")
    else:
        logger.info("No aliases found")


def deleteIndices(es, keep, repository):
    indices = curator.IndexList(es)
    snapshots = indices.indices
    try:
        indices.filter_by_count(
            count=keep,
            reverse=True,
            pattern="^(.*)-\d{8}.*$")
        for snap in snapshots:
            if snap in getAllSnapshots(es, repository):
                snapshot = es.snapshot.get(repository=repository, snapshot=snap)["snapshots"][0]
                if snapshot["state"] != "SUCCESS":
                    indices.filter_by_regex(kind="prefix", value=snap, exclude=True)
                    logger.info("Snapshot %s is not ready. It is in %s state" % (snap, snapshot["state"]))
                else:
                    logger.info("Snapshot %s already exists" % (snap))
            else:
                indices.filter_by_regex(kind="prefix", value=snap, exclude=True)
        deleteIndices = curator.DeleteIndices(indices)
        deleteIndices.do_action()
    except NoIndices as e:
        logger.info("No indices to delete")



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
        rolloverCluster(es, clusterConfig["rollover_conditions"], clusterConfig["exclude_aliases"])
        createRepository(es, cluster, bucket)
        createSnapshots(es, cluster, clusterConfig["exclude_aliases"])
        deleteIndices(es, cluster, clusterConfig["indices_to_keep"])


if __name__ == "__main__":
    handler(None, None)
