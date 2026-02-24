# A script to list compute environments and owner details, number of executions, last used.
# v4: Adds organization name for org-owned environments, and first/last revision details
#     (revision number, created date, author name/email/loginId) from environment_revisions.
import os
import logging
import csv
from pymongo import MongoClient, errors

# Set up logging
logging.basicConfig(filename='environment_data.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')


def get_mongo_client():
    """
    Creates and returns a MongoDB client.
    """
    try:
        user = os.environ.get("MONGODB_USERNAME")
        password = os.environ.get("MONGODB_PASSWORD")
        platform_namespace = 'domino-platform'

        # remove authMechanism='SCRAM-SHA-256' if non admin user is used.
        client = MongoClient(
            'mongodb://mongodb-replicaset.{}.svc.cluster.local:27017'.format(platform_namespace),
            username=user,
            password=password,
            authSource='admin',
            authMechanism='SCRAM-SHA-256'
        )
        logging.info("Successfully connected to MongoDB.")
        return client

    except errors.PyMongoError as e:
        logging.error("Failed to connect to MongoDB: %s", e)
        raise


def get_environment_data(result_limit=500):
    """
    Retrieves environment data with run counts, owner/org details, and
    first/last revision metadata (number, created date, author).
    """
    try:
        client = get_mongo_client()

        db = client['domino']
        environments_v2 = db['environments_v2']

        cursor = environments_v2.aggregate([
            {'$match': {'isArchived': False}},

            # ----------------------------------------------------------------
            # Run counts and last-used info
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'runs',
                'let': {'environmentId': '$_id'},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$environmentId', '$$environmentId']}}},
                    {'$group': {
                        '_id': '$environmentId',
                        'runsCount': {'$sum': 1},
                        'latestStarted': {'$max': '$started'},
                        'startingUserId': {'$first': '$startingUserId'}
                    }}
                ],
                'as': 'runData'
            }},
            {'$addFields': {
                'runsCount': {'$ifNull': [{'$arrayElemAt': ['$runData.runsCount', 0]}, 0]},
                'latestStarted': {'$arrayElemAt': ['$runData.latestStarted', 0]},
                'startingUserId': {'$arrayElemAt': ['$runData.startingUserId', 0]}
            }},

            # ----------------------------------------------------------------
            # Owner user lookup (populated for Private/Global visibility envs)
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'users',
                'localField': 'ownerId',
                'foreignField': '_id',
                'as': 'ownerDetails'
            }},
            {'$unwind': {
                'path': '$ownerDetails',
                'preserveNullAndEmptyArrays': True
            }},

            # ----------------------------------------------------------------
            # Organization lookup (populated for Organization visibility envs)
            # environments_v2.ownerId matches organizations.organizationUserId
            # (not organizations._id). The org's display name is NOT stored in
            # this collection — it lives in users where users._id == organizationUserId,
            # which the ownerDetails lookup above already resolves.
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'organizations',
                'localField': 'ownerId',
                'foreignField': 'organizationUserId',
                'as': 'orgDetails'
            }},
            {'$unwind': {
                'path': '$orgDetails',
                'preserveNullAndEmptyArrays': True
            }},

            # ----------------------------------------------------------------
            # Last-run starting user lookup
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'users',
                'localField': 'startingUserId',
                'foreignField': '_id',
                'as': 'startingUserDetails'
            }},
            {'$unwind': {
                'path': '$startingUserDetails',
                'preserveNullAndEmptyArrays': True
            }},

            # ----------------------------------------------------------------
            # First revision (lowest metadata.number)
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'environment_revisions',
                'let': {'envId': '$_id'},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$environmentId', '$$envId']}}},
                    {'$sort': {'metadata.number': 1}},
                    {'$limit': 1}
                ],
                'as': 'firstRevision'
            }},

            # ----------------------------------------------------------------
            # Last revision (highest metadata.number)
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'environment_revisions',
                'let': {'envId': '$_id'},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$environmentId', '$$envId']}}},
                    {'$sort': {'metadata.number': -1}},
                    {'$limit': 1}
                ],
                'as': 'lastRevision'
            }},

            # Extract revision metadata fields so we can join on authorId
            {'$addFields': {
                'firstRevisionData': {'$arrayElemAt': ['$firstRevision', 0]},
                'lastRevisionData': {'$arrayElemAt': ['$lastRevision', 0]}
            }},
            {'$addFields': {
                'firstRevisionNumber': '$firstRevisionData.metadata.number',
                'firstRevisionCreated': '$firstRevisionData.metadata.created',
                'firstRevisionAuthorId': '$firstRevisionData.metadata.authorId',
                'lastRevisionNumber': '$lastRevisionData.metadata.number',
                'lastRevisionCreated': '$lastRevisionData.metadata.created',
                'lastRevisionAuthorId': '$lastRevisionData.metadata.authorId'
            }},

            # ----------------------------------------------------------------
            # First revision author details
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'users',
                'localField': 'firstRevisionAuthorId',
                'foreignField': '_id',
                'as': 'firstRevisionAuthorDetails'
            }},
            {'$unwind': {
                'path': '$firstRevisionAuthorDetails',
                'preserveNullAndEmptyArrays': True
            }},

            # ----------------------------------------------------------------
            # Last revision author details
            # ----------------------------------------------------------------
            {'$lookup': {
                'from': 'users',
                'localField': 'lastRevisionAuthorId',
                'foreignField': '_id',
                'as': 'lastRevisionAuthorDetails'
            }},
            {'$unwind': {
                'path': '$lastRevisionAuthorDetails',
                'preserveNullAndEmptyArrays': True
            }},

            # ----------------------------------------------------------------
            # Flatten all fields
            # ----------------------------------------------------------------
            {'$addFields': {
                'ownerFullName': '$ownerDetails.fullName',
                'ownerEmail': '$ownerDetails.email',
                'ownerLoginId': '$ownerDetails.loginId.id',
                # Organization name — the organizations collection has no name field.
                # Domino creates a pseudo-user entry per org whose _id == organizationUserId
                # == environments_v2.ownerId. The org's fullName is empty; the name is in
                # loginId.id (e.g. "all-data-scientists", "nyc-data-scientists").
                'organizationName': {
                    '$cond': {
                        'if': {'$eq': ['$visibility', 'Organization']},
                        'then': '$ownerDetails.loginId.id',
                        'else': None
                    }
                },
                'startingUserName': '$startingUserDetails.fullName',
                'startingUserEmail': '$startingUserDetails.email',
                'startingUserLoginId': '$startingUserDetails.loginId.id',
                'firstRevisionAuthorFullName': '$firstRevisionAuthorDetails.fullName',
                'firstRevisionAuthorEmail': '$firstRevisionAuthorDetails.email',
                'firstRevisionAuthorLoginId': '$firstRevisionAuthorDetails.loginId.id',
                'lastRevisionAuthorFullName': '$lastRevisionAuthorDetails.fullName',
                'lastRevisionAuthorEmail': '$lastRevisionAuthorDetails.email',
                'lastRevisionAuthorLoginId': '$lastRevisionAuthorDetails.loginId.id'
            }},

            {'$project': {
                '_id': 0,
                'name': 1,
                'description': 1,
                'visibility': 1,
                'isArchived': 1,
                'runsCount': 1,
                'ownerId': 1,
                'ownerFullName': 1,
                'ownerEmail': 1,
                'ownerLoginId': 1,
                'organizationName': 1,
                'latestStarted': 1,
                'startingUserName': 1,
                'startingUserEmail': 1,
                'startingUserLoginId': 1,
                'firstRevisionNumber': 1,
                'firstRevisionCreated': 1,
                'firstRevisionAuthorFullName': 1,
                'firstRevisionAuthorEmail': 1,
                'firstRevisionAuthorLoginId': 1,
                'lastRevisionNumber': 1,
                'lastRevisionCreated': 1,
                'lastRevisionAuthorFullName': 1,
                'lastRevisionAuthorEmail': 1,
                'lastRevisionAuthorLoginId': 1
            }},

            {'$sort': {'runsCount': -1}}
        ])

        results = list(cursor)[:result_limit]
        logging.info("Successfully retrieved %d documents.", len(results))
        return results

    except errors.PyMongoError as e:
        logging.error("A PyMongo error occurred: %s", e)
        raise
    except Exception as e:
        logging.error("An unexpected error occurred: %s", e)
        raise


def write_to_csv(data, filename='environment_data.csv'):
    """
    Writes the retrieved environment data to a CSV file.
    """
    fieldnames = [
        'name', 'description', 'visibility', 'isArchived', 'runsCount', 'ownerId',
        'ownerFullName', 'ownerEmail', 'ownerLoginId',
        'organizationName',
        'latestStarted',
        'startingUserName', 'startingUserEmail', 'startingUserLoginId',
        'firstRevisionNumber', 'firstRevisionCreated',
        'firstRevisionAuthorFullName', 'firstRevisionAuthorEmail', 'firstRevisionAuthorLoginId',
        'lastRevisionNumber', 'lastRevisionCreated',
        'lastRevisionAuthorFullName', 'lastRevisionAuthorEmail', 'lastRevisionAuthorLoginId'
    ]
    try:
        with open(filename, mode='w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for row in data:
                # Convert ObjectId fields to strings for CSV compatibility
                if 'ownerId' in row and row['ownerId'] is not None:
                    row['ownerId'] = str(row['ownerId'])
                writer.writerow(row)
        logging.info("Data successfully written to %s.", filename)

    except IOError as e:
        logging.error("Failed to write data to CSV file: %s", e)
        raise


def main():
    try:
        result_limit = 500
        data = get_environment_data(result_limit=result_limit)
        write_to_csv(data)
        logging.info("Script completed successfully.")

    except Exception as e:
        logging.error("Script failed with an error: %s", e)


if __name__ == "__main__":
    main()
