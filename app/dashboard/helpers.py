# -*- coding: utf-8 -*-
"""Handle dashboard helpers and related logic.

Copyright (C) 2018 Gitcoin Core

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
import logging
import pprint
from enum import Enum

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone

import requests
from bs4 import BeautifulSoup
from dashboard.models import Bounty, BountyFulfillment, BountySyncRequest
from dashboard.notifications import (
    maybe_market_to_email, maybe_market_to_github, maybe_market_to_slack, maybe_market_to_twitter,
)
from economy.utils import convert_amount
from pytz import UTC
from ratelimit.decorators import ratelimit

from .models import Profile

logger = logging.getLogger(__name__)


# gets amount of remote html doc (github issue)
@ratelimit(key='ip', rate='100/m', method=ratelimit.UNSAFE, block=True)
def amount(request):
    response = {}

    try:
        amount = request.GET.get('amount')
        denomination = request.GET.get('denomination', 'ETH')
        if denomination == 'ETH':
            amount_in_eth = float(amount)
        else:
            amount_in_eth = convert_amount(amount, denomination, 'ETH')
        amount_in_usdt = convert_amount(amount_in_eth, 'ETH', 'USDT')
        response = {
            'eth': amount_in_eth,
            'usdt': amount_in_usdt,
        }
        return JsonResponse(response)
    except Exception as e:
        print(e)
        raise Http404


# gets title of remote html doc (github issue)
@ratelimit(key='ip', rate='10/m', method=ratelimit.UNSAFE, block=True)
def title(request):
    response = {}

    url = request.GET.get('url')
    urlVal = URLValidator()
    try:
        urlVal(url)
    except ValidationError:
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    if url.lower()[:19] != 'https://github.com/':
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    try:
        html_response = requests.get(url)
    except ValidationError:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)

    title = None
    try:
        soup = BeautifulSoup(html_response.text, 'html.parser')

        eles = soup.findAll("span", {"class": "js-issue-title"})
        if len(eles):
            title = eles[0].text

        if not title and soup.title:
            title = soup.title.text

        if not title:
            for link in soup.find_all('h1'):
                print(link.text)

    except ValidationError:
        response['message'] = 'could not parse html'
        return JsonResponse(response)

    try:
        response['title'] = title.replace('\n', '').strip()
    except Exception as e:
        print(e)
        response['message'] = 'could not find a title'

    return JsonResponse(response)


# gets description of remote html doc (github issue)
@ratelimit(key='ip', rate='10/m', method=ratelimit.UNSAFE, block=True)
def description(request):
    response = {}

    url = request.GET.get('url')
    urlVal = URLValidator()
    try:
        urlVal(url)
    except ValidationError as e:
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    if url.lower()[:19] != 'https://github.com/':
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    # Web format:  https://github.com/jasonrhaas/slackcloud/issues/1
    # API format:  https://api.github.com/repos/jasonrhaas/slackcloud/issues/1
    gh_api = url.replace('github.com', 'api.github.com/repos')

    try:
        api_response = requests.get(gh_api)
    except ValidationError as e:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)

    if api_response.status_code != 200:
        response['message'] = 'there was a problem reaching the github api'
        return JsonResponse(response)

    try:
        body = api_response.json()['body']
    except ValueError as e:
        response['message'] = e
    except KeyError as e:
        response['message'] = e
    else:
        response['description'] = body.replace('\n', '').strip()

    return JsonResponse(response)


# gets keywords of remote issue (github issue)
@ratelimit(key='ip', rate='10/m', method=ratelimit.UNSAFE, block=True)
def keywords(request):
    response = {}
    keywords = []

    url = request.GET.get('url')
    urlVal = URLValidator()
    try:
        urlVal(url)
    except ValidationError:
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    if url.lower()[:19] != 'https://github.com/':
        response['message'] = 'invalid arguments'
        return JsonResponse(response)

    try:
        repo_url = None
        if '/pull' in url:
            repo_url = url.split('/pull')[0]
        if '/issue' in url:
            repo_url = url.split('/issue')[0]
        split_repo_url = repo_url.split('/')
        keywords.append(split_repo_url[-1])
        keywords.append(split_repo_url[-2])

        html_response = requests.get(repo_url)
    except ValidationError:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)
    except AttributeError:
        response['message'] = 'could not pull back remote response'
        return JsonResponse(response)

    try:
        soup = BeautifulSoup(html_response.text, 'html.parser')

        eles = soup.findAll("span", {"class": "lang"})
        for ele in eles:
            keywords.append(ele.text)

    except ValidationError:
        response['message'] = 'could not parse html'
        return JsonResponse(response)

    try:
        response['keywords'] = keywords
    except Exception as e:
        print(e)
        response['message'] = 'could not find a title'

    return JsonResponse(response)


def normalizeURL(url):
    if url[-1] == '/':
        url = url[0:-1]
    return url


# returns did_change if bounty has changed since last sync
# then old_bounty
# then new_bounty
def syncBountywithWeb3(bountyContract, url, network):
    bountydetails = bountyContract.call().bountydetails(url)
    return process_bounty_details(bountydetails, url, bountyContract.address, network)


class BountyStage(Enum):
    """Python enum class that matches up with the Standard Bounties BountyStage enum.

    Attributes:
        Draft (int): Bounty is a draft.
        Active (int): Bounty is active.
        Dead (int): Bounty is dead.

    """

    Draft = 0
    Active = 1
    Dead = 2


class UnsupportedSchemaException(Exception):
    pass


def bounty_did_change(bounty_id, new_bounty_details):
    """Determine whether or not the Bounty has changed.

    Args:
        bounty_id (int): The ID of the Bounty.
        new_bounty_details (dict): The new Bounty raw data JSON.

    Returns:
        bool: Whether or not the Bounty has changed.
        QuerySet: The old bounties queryset.

    """
    did_change = False
    old_bounties = Bounty.objects.none()
    try:
        old_bounties = Bounty.objects.current() \
            .filter(standard_bounties_id=bounty_id).order_by('-created_on')
        did_change = (new_bounty_details != old_bounties.first().raw_data)
    except Exception:
        did_change = True

    print('* Bounty did_change:', did_change)

    return did_change, old_bounties


def handle_bounty_fulfillments(fulfillments, new_bounty):
    """Handle BountyFulfillment creation for new bounties.

    Args:
        fulfillments (dict): The fulfillments data dictionary.
        new_bounty (dashboard.models.Bounty): The new Bounty object.

    Returns:
        QuerySet: The BountyFulfillments queryset.

    """
    for fulfillment in fulfillments:
        kwargs = {}
        github_username = fulfillment.get('data', {}).get(
            'payload', {}).get('fulfiller', {}).get(
                'githubUsername', '')
        if github_username:
            try:
                kwargs['profile_id'] = Profile.objects.get(
                    handle=github_username).pk
            except Profile.DoesNotExist:
                pass
        if fulfillment.get('accepted'):
            kwargs['accepted'] = True
        try:
            new_fulfillment = BountyFulfillment.objects.create(
                fulfiller_address=fulfillment.get(
                    'fulfiller',
                    '0x0000000000000000000000000000000000000000'),
                fulfiller_email=fulfillment.get('data', {}).get(
                    'payload', {}).get('fulfiller', {}).get('email', ''),
                fulfiller_github_username=github_username,
                fulfiller_name=fulfillment.get('data', {}).get(
                    'payload', {}).get('fulfiller', {}).get('name', ''),
                fulfiller_metadata=fulfillment,
                fulfillment_id=fulfillment.get('id'),
                bounty=new_bounty,
                **kwargs)
            new_fulfillment.save()
            new_bounty.fulfillments.add(new_fulfillment)
        except Exception as e:
            logging.error(f'{e} during new fulfillment creation for {new_bounty}')
            continue
    return new_bounty.fulfillments.all()


def create_new_bounty(old_bounties, bounty_payload, bounty_details, bounty_id):
    """Handle new Bounty creation in the event of bounty changes.

    Possible Bounty Stages:
        0: Draft
        1: Active
        2: Dead

    Returns:
        dashboard.models.Bounty: The new Bounty object.

    """
    bounty_issuer = bounty_payload.get('issuer', {})
    metadata = bounty_payload.get('metadata', {})
    # fulfillments metadata will be empty when bounty is first created
    fulfillments = bounty_details.get('fulfillments', {})
    submissions_comment_id = None

    # start to process out all the bounty data
    url = bounty_payload.get('webReferenceURL')
    if url:
        url = normalizeURL(url)
    else:
        raise UnsupportedSchemaException('No webReferenceURL found. Cannot continue!')

    # Check if we have any fulfillments.  If so, check if they are accepted.
    # If there are no fulfillments, accepted is automatically False.
    # Currently we are only considering the latest fulfillment.  Std bounties supports multiple.
    # If any of the fulfillments have been accepted, the bounty is now accepted and complete.
    accepted = any(
        [fulfillment.get('accepted') for fulfillment in fulfillments])

    with transaction.atomic():
        for old_bounty in old_bounties:
            if old_bounty.current_bounty:
                submissions_comment_id = old_bounty.submissions_comment
            old_bounty.current_bounty = False
            old_bounty.save()
        try:
            new_bounty = Bounty.objects.create(
                title=bounty_payload.get('title', ''),
                issue_description=bounty_payload.get('description', ' '),
                web3_created=timezone.make_aware(
                    timezone.datetime.fromtimestamp(bounty_payload.get('created')),
                    timezone=UTC),
                value_in_token=bounty_details.get('fulfillmentAmount'),
                token_name=bounty_payload.get('tokenName', ''),
                token_address=bounty_payload.get(
                    'tokenAddress', '0x0000000000000000000000000000000000000000'),
                bounty_type=metadata.get('bountyType', ''),
                project_length=metadata.get('projectLength', ''),
                experience_level=metadata.get('experienceLevel', ''),
                github_url=url,  # Could also use payload.get('webReferenceURL')
                bounty_owner_address=bounty_issuer.get('address', ''),
                bounty_owner_email=bounty_issuer.get('email', ''),
                bounty_owner_github_username=bounty_issuer.get('githubUsername', ''),
                bounty_owner_name=bounty_issuer.get('name', ''),
                is_open=True if (bounty_details.get('bountyStage') == 1
                                 and not accepted) else False,
                raw_data=bounty_details,
                metadata=metadata,
                current_bounty=True,
                contract_address=bounty_details.get('token'),
                network=bounty_details.get('network'),
                accepted=accepted,
                submissions_comment=submissions_comment_id,
                # These fields are after initial bounty creation, in bounty_details.js
                expires_date=timezone.make_aware(
                    timezone.datetime.fromtimestamp(bounty_details.get('deadline')),
                    timezone=UTC),
                standard_bounties_id=bounty_id,
                balance=bounty_details.get('balance'),
                num_fulfillments=len(fulfillments),
            )
            new_bounty.fetch_issue_item()
            if not new_bounty.avatar_url:
                new_bounty.avatar_url = new_bounty.get_avatar_url()
            new_bounty.save()

            # Pull the interested parties off the last old_bounty
            if old_bounties.exists():
                for interested in old_bounties.order_by('-pk').first().interested.all():
                    new_bounty.interested.add(interested)
        except Exception as e:
            print(e, 'encountered during new bounty creation for:', url)
            logging.error(f'{e} encountered during new bounty creation for: {url}')
            new_bounty = None

        if fulfillments:
            handle_bounty_fulfillments(fulfillments, new_bounty)
            for inactive in Bounty.objects.filter(current_bounty=False, github_url=url).order_by('-created_on'):
                BountyFulfillment.objects.filter(bounty_id=inactive.id).delete()
    return new_bounty


def process_bounty_details(bounty_details):
    """Process bounty details.

    Args:
        bounty_details (dict): The Bounty details.

    Raises:
        UnsupportedSchemaException: Exception raised if the schema is unknown
            or unsupported.

    Returns:
        tuple: A tuple of bounty change data.
        tuple[0] (bool): Whether or not the Bounty changed.
        tuple[1] (dashboard.models.Bounty): The first old bounty object.
        tuple[2] (dashboard.models.Bounty): The new Bounty object.

    """
    # See dashboard/utils.py:get_bounty from details on this data
    bounty_id = bounty_details.get('id', {})
    bounty_data = bounty_details.get('data') or {}
    bounty_payload = bounty_data.get('payload', {})
    meta = bounty_data.get('meta', {})

    # what schema are we workign with?
    schema_name = meta.get('schemaName')
    schema_version = meta.get('schemaVersion', 'Unknown')

    if not schema_name:
        raise UnsupportedSchemaException(
            f'Unknown Schema: Unknown - Version: {schema_version}')

    # Create new bounty (but only if things have changed)
    did_change, old_bounties = bounty_did_change(bounty_id, bounty_details)
    if not did_change:
        return (did_change, old_bounties.first(), old_bounties.first())

    new_bounty = create_new_bounty(old_bounties, bounty_payload, bounty_details, bounty_id)

    if new_bounty:
        return (did_change, old_bounties.first(), new_bounty)
    return (did_change, old_bounties.first(), old_bounties.first())


def process_bounty_changes(old_bounty, new_bounty):
    """Process Bounty changes.

    Args:
        old_bounty (dashboard.models.Bounty): The old Bounty object.
        new_bounty (dashboard.models.Bounty): The new Bounty object.

    """
    from dashboard.utils import build_profile_pairs
    profile_pairs = None
    # process bounty sync requests
    did_bsr = False
    for bsr in BountySyncRequest.objects.filter(processed=False, github_url=new_bounty.github_url):
        did_bsr = True
        bsr.processed = True
        bsr.save()

    # new bounty
    if (not old_bounty and new_bounty and new_bounty.is_open) or (not old_bounty.is_open and new_bounty.is_open):
        event_name = 'new_bounty'
    elif old_bounty.num_fulfillments < new_bounty.num_fulfillments:
        event_name = 'work_submitted'
    elif old_bounty.is_open and not new_bounty.is_open:
        if new_bounty.status == 'cancelled':
            event_name = 'killed_bounty'
        else:
            event_name = 'work_done'
    else:
        event_name = 'unknown_event'
    print(event_name)

    # Build profile pairs list
    if new_bounty.fulfillments.exists():
        profile_pairs = build_profile_pairs(new_bounty)

    # marketing
    if event_name != 'unknown_event':
        print("============ posting ==============")
        did_post_to_twitter = maybe_market_to_twitter(new_bounty, event_name)
        did_post_to_slack = maybe_market_to_slack(new_bounty, event_name)
        did_post_to_github = maybe_market_to_github(new_bounty, event_name, profile_pairs)
        did_post_to_email = maybe_market_to_email(new_bounty, event_name)
        print("============ done posting ==============")

        # what happened
        what_happened = {
            'did_bsr': did_bsr,
            'did_post_to_email': did_post_to_email,
            'did_post_to_github': did_post_to_github,
            'did_post_to_slack': did_post_to_slack,
            'did_post_to_twitter': did_post_to_twitter,
        }

        print("changes processed: ")
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(what_happened)
    else:
        print('No notifications sent - Event Type Unknown = did_bsr: ', did_bsr)
