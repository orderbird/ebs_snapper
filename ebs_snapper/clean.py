# -*- coding: utf-8 -*-
#
# Copyright 2016 Rackspace US, Inc.
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
"""Module for cleaning up snapshots."""

from __future__ import print_function
from time import sleep
from datetime import timedelta
import datetime
import json
import logging
import boto3
from ebs_snapper import utils, dynamo, timeout_check

LOG = logging.getLogger()


def perform_fanout_all_regions(context, cli=False):
    """For every region, run the supplied function"""
    # get regions, regardless of instances
    sns_topic = utils.get_topic_arn('CleanSnapshotTopic')
    LOG.debug('perform_fanout_all_regions using SNS topic %s', sns_topic)

    regions = utils.get_regions(must_contain_instances=True)
    for region in regions:
        sleep(5)  # API limit relief
        send_fanout_message(context, region=region, topic_arn=sns_topic, cli=cli)

    LOG.info('Function clean_perform_fanout_all_regions completed')


def send_fanout_message(context, region, topic_arn, cli=False):
    """Publish an SNS message to topic_arn that specifies a region to review snapshots on"""
    message = json.dumps({'region': region})
    LOG.debug('send_fanout_message: %s', message)

    if cli:
        clean_snapshot(context, region)
    else:
        utils.sns_publish(TopicArn=topic_arn, Message=message)
    LOG.info('Function clean_send_fanout_message completed')


def clean_snapshot(context, region, default_min_snaps=5, installed_region='us-east-1'):
    """Check the region see if we should clean up any snapshots"""
    LOG.info('clean_snapshot in region %s', region)
    ec2 = boto3.client('ec2', region_name=region)

    # fetch these, in case we need to figure out what applies to an instance
    configurations = dynamo.list_configurations(context, installed_region)
    LOG.info('Fetched all possible configuration rules from DynamoDB')

    # figure out what dates we want to nuke
    today = datetime.date.today()
    delete_on_values = []
    for i in range(0, 7):  # seven days ago until today
        del_date = today + timedelta(days=-i)
        delete_on_values.append(del_date.strftime('%Y-%m-%d'))

    # setup counters before we start
    deleted_count = 0

    # setup our filters
    filters = [
        {'Name': 'tag-key', 'Values': ['DeleteOn']},
        {'Name': 'tag-value', 'Values': delete_on_values},
    ]
    params = {'Filters': filters}

    # paginate the snapshot list
    tag_paginator = ec2.get_paginator('describe_snapshots')
    for page in tag_paginator.paginate(**params):
        # stop if we're running out of time
        if timeout_check(context, 'clean_snapshot'):
            break

        # if we don't get even a page of results, or missing hash key, skip
        if not page and 'Snapshots' not in page:
            continue

        for snap in page['Snapshots']:
            # stop if we're running out of time
            if timeout_check(context, 'clean_snapshot'):
                break

            # ugly comprehension to strip out a tag
            delete_on = [r['Value'] for r in snap['Tags'] if r.get('Key') == 'DeleteOn'][0]

            # attempt to identify the instance this applies to, so we can check minimums
            try:
                snapshot_volume = snap['VolumeId']
                volume_instance = utils.get_instance_by_volume(snapshot_volume, region)

                # minimum required
                if volume_instance is None:
                    minimum_snaps = default_min_snaps
                else:
                    snapshot_settings = utils.get_snapshot_settings_by_instance(
                        volume_instance, configurations, region)
                    minimum_snaps = snapshot_settings['snapshot']['minimum']

                # current number of snapshots
                no_snaps = utils.count_snapshots(snapshot_volume, region)

                # if we have less than the minimum, don't delete this one
                if no_snaps < minimum_snaps:
                    LOG.warn('Not deleting snapshot %s from %s (%s)',
                             snap['SnapshotId'], region, delete_on)
                    LOG.warn('Only %s snapshots exist, below minimum of %s',
                             no_snaps, minimum_snaps)
                    continue

            except:
                # if we couldn't figure out a minimum of snapshots,
                # don't clean this up -- these could be orphaned snapshots
                LOG.warn('Error analyzing snapshot %s from %s, skipping... (%s)',
                         snap['SnapshotId'],
                         region,
                         delete_on
                        )
                continue

            LOG.warn('Deleting snapshot %s from %s (%s)',
                     snap['SnapshotId'],
                     region,
                     delete_on
                    )
            deleted_count += utils.delete_snapshot(snap['SnapshotId'], region)

    if deleted_count <= 0:
        LOG.warn('No snapshots were cleaned up for the entire region %s', region)
    else:
        LOG.info('Function clean_snapshots_tagged completed, deleted count: %s', str(deleted_count))

    LOG.info('Function clean_snapshot completed')
