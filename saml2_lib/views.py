#!/usr/bin/env python
# -*- coding:utf-8 -*-


from saml2 import (
    BINDING_HTTP_POST,
    BINDING_HTTP_REDIRECT,
    entity,
)
from saml2.client import Saml2Client
from saml2.config import Config as Saml2Config

from django import get_version
from pkg_resources import parse_version
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout, get_user_model
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.template import TemplateDoesNotExist
from django.http import HttpResponseRedirect
from django.utils.http import is_safe_url

from rest_auth.utils import jwt_encode

import os
import json

# default User or custom User. Now both will work.
User = get_user_model()

try:
    import urllib2 as _urllib
except:
    import urllib.request as _urllib
    import urllib.error
    import urllib.parse

if parse_version(get_version()) >= parse_version('1.7'):
    from django.utils.module_loading import import_string
else:
    from django.utils.module_loading import import_by_path as import_string


def _default_next_url(saml2_auth):
    if 'DEFAULT_NEXT_URL' in saml2_auth:
        return saml2_auth['DEFAULT_NEXT_URL']
    # Lazily evaluate this in case we don't have admin loaded.
    return get_reverse('admin:index')


def get_current_domain(r, saml2_auth):
    if 'ASSERTION_URL' in saml2_auth:
        return saml2_auth['ASSERTION_URL']
    return '{scheme}://{host}'.format(
        scheme='https' if r.is_secure() else 'http',
        host=r.get_host(),
    )


def get_reverse(objs, company=None, app=None):
    '''In order to support different django version, I have to do this '''
    if parse_version(get_version()) >= parse_version('2.0'):
        from django.urls import reverse
    else:
        from django.core.urlresolvers import reverse
    if objs.__class__.__name__ not in ['list', 'tuple']:
        objs = [objs]
    if company and app:
        kwargs = {
            'company': company,
            'app': app,
        }
    else:
        kwargs = None
    for obj in objs:
        try:
            return reverse(obj, kwargs=kwargs)
        except:
            pass
    raise Exception('We got a URL reverse issue: %s. This is a known issue but please still submit a ticket at https://github.com/fangli/django-saml2-auth/issues/new' % str(objs))


def _get_metadata(saml2_auth):
    if 'METADATA_LOCAL_FILE_PATH' in saml2_auth:
        return {
            'local': [saml2_auth['METADATA_LOCAL_FILE_PATH']]
        }
    else:
        return {
            'remote': [
                {
                    "url": saml2_auth['METADATA_AUTO_CONF_URL'],
                },
            ]
        }


def _get_saml_client(domain, company, app, saml2_auth):
    acs_url = domain + get_reverse([acs, 'acs', 'saml2_lib:acs'], company, app)
    metadata = _get_metadata(saml2_auth)

    saml_settings = {
        'metadata': metadata,
        'service': {
            'sp': {
                'endpoints': {
                    'assertion_consumer_service': [
                        (acs_url, BINDING_HTTP_REDIRECT),
                        (acs_url, BINDING_HTTP_POST)
                    ],
                },
                'allow_unsolicited': True,
                'authn_requests_signed': False,
                'logout_requests_signed': True,
                'want_assertions_signed': True,
                'want_response_signed': False,
            },
        },
    }

    if 'ENTITY_ID' in saml2_auth:
        saml_settings['entityid'] = saml2_auth['ENTITY_ID']

    if 'NAME_ID_FORMAT' in saml2_auth:
        saml_settings['service']['sp']['name_id_format'] = saml2_auth['NAME_ID_FORMAT']

    spConfig = Saml2Config()
    spConfig.load(saml_settings)
    spConfig.allow_unknown_attributes = True
    saml_client = Saml2Client(config=spConfig)
    return saml_client


@login_required
def welcome(r, saml2_auth):
    try:
        return render(r, 'saml2_lib/welcome.html', {'user': r.user})
    except TemplateDoesNotExist:
        return HttpResponseRedirect(_default_next_url(saml2_auth))


def denied(r):
    return render(r, 'saml2_lib/denied.html')


def _create_new_user(username, email, firstname, lastname, saml2_auth):
    user = User.objects.create_user(username, email)
    user.first_name = firstname
    user.last_name = lastname
    groups = [Group.objects.get(name=x) for x in saml2_auth.get('NEW_USER_PROFILE', {}).get('USER_GROUPS', [])]
    if parse_version(get_version()) >= parse_version('2.0'):
        user.groups.set(groups)
    else:
        user.groups = groups
    user.is_active = saml2_auth.get('NEW_USER_PROFILE', {}).get('ACTIVE_STATUS', True)
    user.is_staff = saml2_auth.get('NEW_USER_PROFILE', {}).get('STAFF_STATUS', True)
    user.is_superuser = saml2_auth.get('NEW_USER_PROFILE', {}).get('SUPERUSER_STATUS', False)
    user.save()
    return user


@csrf_exempt
def acs(r, company, app):
    saml2_auth = json.load(open('.'.join([company, app, 'json']), 'r'))
    saml_client = _get_saml_client(get_current_domain(r, saml2_auth), company, app, saml2_auth)
    resp = r.POST.get('SAMLResponse', None)
    next_url = r.session.get('login_next_url', _default_next_url(saml2_auth))

    if not resp:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'saml2_lib:denied']))

    authn_response = saml_client.parse_authn_request_response(
        resp, entity.BINDING_HTTP_POST)
    if authn_response is None:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'saml2_lib:denied']))

    user_identity = authn_response.get_identity()
    if user_identity is None:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'saml2_lib:denied']))

    user_email = user_identity[saml2_auth.get('ATTRIBUTES_MAP', {}).get('email', 'Email')][0]
    user_name = user_identity[saml2_auth.get('ATTRIBUTES_MAP', {}).get('username', 'UserName')][0]
    user_first_name = user_identity[saml2_auth.get('ATTRIBUTES_MAP', {}).get('first_name', 'FirstName')][0]
    user_last_name = user_identity[saml2_auth.get('ATTRIBUTES_MAP', {}).get('last_name', 'LastName')][0]

    target_user = None
    is_new_user = False

    try:
        target_user = User.objects.get(username=user_name)
        if saml2_auth.get('TRIGGER', {}).get('BEFORE_LOGIN', None):
            import_string(saml2_auth['TRIGGER']['BEFORE_LOGIN'])(user_identity)
    except User.DoesNotExist:
        new_user_should_be_created = saml2_auth.get('CREATE_USER', True)
        if new_user_should_be_created: 
            target_user = _create_new_user(user_name, user_email, user_first_name, user_last_name, saml2_auth)
            if saml2_auth.get('TRIGGER', {}).get('CREATE_USER', None):
                import_string(saml2_auth['TRIGGER']['CREATE_USER'])(user_identity)
            is_new_user = True
        else:
            return HttpResponseRedirect(get_reverse([denied, 'denied', 'saml2_lib:denied']))

    r.session.flush()

    if target_user.is_active:
        target_user.backend = 'django.contrib.auth.backends.ModelBackend'
        login(r, target_user)
    else:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'saml2_lib:denied']))

    if saml2_auth.get('USE_JWT') is True:
        # We use JWT auth send token to frontend
        jwt_token = jwt_encode(target_user)
        query = '?uid={}&token={}'.format(target_user.id, jwt_token)

        frontend_url = saml2_auth.get(
            'FRONTEND_URL', next_url)

        return HttpResponseRedirect(frontend_url+query)

    if is_new_user:
        try:
            return render(r, 'saml2_lib/welcome.html', {'user': r.user})
        except TemplateDoesNotExist:
            return HttpResponseRedirect(next_url)
    else:
        return HttpResponseRedirect(next_url)


def signin(r, company, app, saml2_auth):
    try:
        import urlparse as _urlparse
        from urllib import unquote
    except:
        import urllib.parse as _urlparse
        from urllib.parse import unquote
    next_url = r.GET.get('next', _default_next_url(saml2_auth))

    try:
        if 'next=' in unquote(next_url):
            next_url = _urlparse.parse_qs(_urlparse.urlparse(unquote(next_url)).query)['next'][0]
    except:
        next_url = r.GET.get('next', _default_next_url(saml2_auth))

    # Only permit signin requests where the next_url is a safe URL
    if parse_version(get_version()) >= parse_version('2.0'):
        url_ok = is_safe_url(next_url, None)
    else:
        url_ok = is_safe_url(next_url, saml2_auth)

    if not url_ok:
        return HttpResponseRedirect(get_reverse([denied, 'denied', 'saml2_lib:denied']))

    r.session['login_next_url'] = next_url

    saml_client = _get_saml_client(get_current_domain(r, saml2_auth), company, app, saml2_auth)
    _, info = saml_client.prepare_for_authenticate()

    redirect_url = None

    for key, value in info['headers']:
        if key == 'Location':
            redirect_url = value
            break

    return HttpResponseRedirect(redirect_url)


def signout(r):
    logout(r)
    return render(r, 'saml2_lib/signout.html')