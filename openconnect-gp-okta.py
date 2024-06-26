#!/usr/bin/env python3

import base64
import click
import configparser
import contextlib
import json
import lxml.etree
import os
import re
import requests
import shlex
import signal
import ssl
import subprocess
import sys
import urllib

try:
    import pyotp
except ImportError:
    pyotp = None

HAS_FIDO2 = True
try:
    from fido2.client import Fido2Client, UserInteraction
    from fido2.hid import list_devices
    from fido2.utils import websafe_decode, websafe_encode
except ImportError:
    HAS_FIDO2 = False
    UserInteraction = object


class ConsoleInteraction(UserInteraction):
    def prompt_up(self):
        """Called when the authenticator is awaiting a user presence check."""
        click.echo("Touch your hardware token to confirm user presence")

    def request_pin(self, permissions, rp_id):
        """Called when the client requires a PIN from the user.
        Should return a PIN, or None/Empty to cancel."""
        return click.prompt("Enter your hardware token pin", hide_input=True)

    def request_uv(self, permissions, rp_id):
        """Called when the client is about to request UV from the user.
        Should return True if allowed, or False to cancel."""
        click.echo("User Verification requested.")
        return True

class OktaWebauthn:
    # Not using factor['_links']['verify']['href'] as multiple
    # webauthn devices may exist and Okta has an alternative
    # workflow which works with any webauthn device available by
    # querying a generic URL.
    WEBAUTHN_URL_TEMPLATE = 'https://{}/api/v1/authn/factors/webauthn/verify'

    def __init__(self):
        assert HAS_FIDO2
        self._device = None

    def get_device(self):
        self._device = next(list_devices(), None)
        while self._device is None:
            click.echo("Please insert a suitable device if you wish to continue with webauthn MFA.")
            if click.confirm("Continue with webauthn MFA?"):
                self._device = next(list_devices(), None)
            else:
                click.echo("Falling back to other MFA method.")
                break
        return self._device is not None

    def okta_verify(self, session, domain, stateToken):
        url = self.WEBAUTHN_URL_TEMPLATE.format(domain)
        r = post_json(session, url, {'stateToken': stateToken})
        assert r['status'] == 'MFA_CHALLENGE'

        fido_client = Fido2Client(
            self._device,
            "https://{}".format(domain),
            user_interaction=ConsoleInteraction(),
        )
        pubkey_req = {
            "challenge": websafe_decode(r['_embedded']['challenge']['challenge']),
            "rpId": domain,
            'allowCredentials': [
                {
                    'type': 'public-key',
                    'id': websafe_decode(f['profile']['credentialId'])
                } for f in r['_embedded']['factors']
            ]
        }

        assertion = fido_client.get_assertion(pubkey_req).get_response(0)

        def b64(obj):
            # websafe_encode(obj) takes a python object and serializes it using
            # methods defined by this given object so that (obj !=
            # websafe_decode(websafe_encode(obj))). The latter is a bytestring
            # that then needs to be correctly base64 encoded for Okta.
            return base64.b64encode(websafe_decode(websafe_encode(obj))).decode('ascii')

        next_url = r['_links']['next']['href']
        payload = {
            'authenticatorData': b64(assertion['authenticatorData']),
            'clientData': b64(assertion['clientData']),
            'signatureData': b64(assertion['signature']),
            'stateToken': r["stateToken"]
        }
        return post_json(session, next_url, payload)


def check(r):
    r.raise_for_status()
    return r

def extract_form(html):
    form = lxml.etree.fromstring(html, lxml.etree.HTMLParser()).find('.//form')
    return (form.attrib['action'],
        {inp.attrib['name']: inp.attrib['value'] for inp in form.findall('input')})

def prelogin(s, gateway):
    r = check(s.post('https://{}/ssl-vpn/prelogin.esp'.format(gateway)))
    saml_req_html = base64.b64decode(lxml.etree.fromstring(r.content).find('saml-request').text)
    saml_req_url, saml_req_data = extract_form(saml_req_html)
    assert 'SAMLRequest' in saml_req_data
    return saml_req_url + '?' + urllib.parse.urlencode(saml_req_data)

def post_json(s, url, data):
    r = check(s.post(url, data=json.dumps(data),
        headers={'Content-Type': 'application/json'}))
    return r.json()

def okta_auth(s, domain, username, password, factor_priorities, totp_key):
    r = post_json(s, 'https://{}/api/v1/authn'.format(domain),
        {'username': username, 'password': password})

    if r['status'] == 'MFA_REQUIRED':
        def priority(factor):
            return factor_priorities.get(factor['factorType'], 0)

        ignore_webauthn = not HAS_FIDO2
        for factor in sorted(r['_embedded']['factors'], key=priority, reverse=True):
            if factor['factorType'] == 'push':
                url = factor['_links']['verify']['href']
                correct_answer = None
                while True:
                    r = post_json(s, url, {'stateToken': r['stateToken']})
                    if r['status'] != 'MFA_CHALLENGE':
                        break
                    assert r['factorResult'] == 'WAITING'
                    if correct_answer is None:
                        try:
                            correct_answer = r["_embedded"]["factor"]["_embedded"]["challenge"]["correctAnswer"]
                            click.echo(f"Correct 3-number answer is: {correct_answer}")
                        except KeyError:
                            pass
                break
            if factor['factorType'] == 'sms':
                url = factor['_links']['verify']['href']
                r = post_json(s, url, {'stateToken': r['stateToken']})
                assert r['status'] == 'MFA_CHALLENGE'
                code = click.prompt('SMS code')
                r = post_json(s, url, {'stateToken': r['stateToken'], 'passCode': code})
                break
            if factor['factorType'] == 'webauthn' and not ignore_webauthn:
                webauthn = OktaWebauthn()
                if webauthn.get_device():
                    r = webauthn.okta_verify(s, domain, r['stateToken'])
                else:
                    ignore_webauthn = True
                    continue
                break
            if re.match('token(?::|$)', factor['factorType']):
                url = factor['_links']['verify']['href']
                if (factor['factorType'] == 'token:software:totp') and (totp_key is not None):
                    code = pyotp.TOTP(totp_key).now()
                else:
                    code = click.prompt('One-time code for {} ({})'.format(factor['provider'], factor['vendorName']))
                r = post_json(s, url, {'stateToken': r['stateToken'], 'passCode': code})
                break
        else:
            raise Exception('No supported authentication factors')

    if r['status'] == 'LOCKED_OUT':
        raise Exception('Locked out of Okta!')
    assert r['status'] == 'SUCCESS'
    return r['sessionToken']

def okta_saml(s, saml_req_url, username, password, factor_priorities, totp_key):
    domain = urllib.parse.urlparse(saml_req_url).netloc

    # Just to set DT cookie
    check(s.get(saml_req_url))

    token = okta_auth(s, domain, username, password, factor_priorities, totp_key)

    r = check(s.get('https://{}/login/sessionCookieRedirect'.format(domain),
        params={'token': token, 'redirectUrl': saml_req_url}))
    saml_resp_url, saml_resp_data = extract_form(r.content)
    assert 'SAMLResponse' in saml_resp_data
    return saml_resp_url, saml_resp_data

def complete_saml(s, saml_resp_url, saml_resp_data):
    r = check(s.post(saml_resp_url, data=saml_resp_data))
    return r.headers['saml-username'], r.headers['prelogin-cookie']

@contextlib.contextmanager
def signal_mask(how, mask):
    old_mask = signal.pthread_sigmask(how, mask)
    try:
        yield old_mask
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

@contextlib.contextmanager
def signal_handler(num, handler):
    old_handler = signal.signal(num, handler)
    try:
        yield old_handler
    finally:
        signal.signal(num, old_handler)

@contextlib.contextmanager
def popen_forward_sigterm(args, *, stdin=None):
    with signal_mask(signal.SIG_BLOCK, {signal.SIGTERM}) as old_mask:
        with subprocess.Popen(args, stdin=stdin,
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)) as p:
            with signal_handler(signal.SIGTERM, lambda *args: p.terminate()):
                with signal_mask(signal.SIG_SETMASK, old_mask):
                    yield p
                    if p.stdin:
                        p.stdin.close()
                    os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)

def run_cmd(cmd, confvar):
    out = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    output = out.stdout.splitlines()
    if out.returncode != 0 or len(output) == 0:
        click.echo(f"{confvar} command failed with return status {out.returncode}:", err=True)
        click.echo(out.stderr, nl=False, err=True)
    else:
        if len(output) > 1:
            click.echo(
                "{confvar} command produced more than"
                "one line of output, using the first one"
            )
        return out.stdout.splitlines()[0]
    return None

class TLSAdapter(requests.adapters.HTTPAdapter):

        def init_poolmanager(self, *args, **kwargs):
            ctx = ssl.create_default_context()
            ctx.options |= 0x4
            kwargs['ssl_context'] = ctx
            return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

@click.command()
@click.argument('gateway', required=False)
@click.argument('openconnect-args', nargs=-1)
@click.option('--config', default="")
@click.option('--username')
@click.option('--password')
@click.option('--password-cmd')
@click.option('--factor-priority', 'factor_priorities', nargs=2, type=click.Tuple((str, int)), multiple=True)
@click.option('--totp-key')
@click.option('--totp-key-cmd')
@click.option('--sudo/--no-sudo', default=None)
def main(
    gateway,
    openconnect_args,
    config,
    username,
    password,
    password_cmd,
    factor_priorities,
    totp_key,
    totp_key_cmd,
    sudo,
):
    args = { k: v for k, v in locals().items() if v is not None }

    conf = configparser.ConfigParser()
    conf.optionxform = lambda opt: opt.replace('_', '-')
    conf.read(config)

    gateway = conf.get('common', 'gateway', vars=args, fallback=gateway)
    openconnect_args += tuple(shlex.split(conf.get('common', 'openconnect-args', fallback="")))
    username = conf.get('common', 'username', vars=args, fallback=username)
    password = conf.get('common', 'password', vars=args, fallback=password)
    password_cmd = conf.get('common', 'password-cmd', vars=args, fallback=password_cmd)
    totp_key = conf.get('common', 'totp-key', vars=args, fallback=totp_key)
    totp_key_cmd = conf.get('common', 'totp-key-cmd', vars=args, fallback=totp_key)
    sudo = conf.get('common', 'sudo', vars=args, fallback=False if sudo is None else sudo)

    if conf.has_section('factor-priority'):
        factor_priorities += tuple(
            map(
                lambda x: (x[0], int(x[1])),
                conf.items('factor-priority')
            )
        )

    if (totp_key_cmd is not None):
        totp_key = run_cmd(totp_key_cmd, "TOTP")
    if (totp_key is not None) and (pyotp is None):
        click.echo('--totp-key requires pyotp!', err=True)
        sys.exit(1)

    if gateway is None:
        click.echo('No gateway provided', err=True)
        sys.exit(1)

    if username is None:
        username = click.prompt('Username')
    if password_cmd is not None:
        password = run_cmd(password_cmd, "Password")
    if password is None:
        password = click.prompt('Password', hide_input=True)

    factor_priorities = {
        'token:software:totp': 0 if totp_key is None else 2,
        'push': 1,
        **dict(factor_priorities)}

    with requests.Session() as s:
        s.mount('https://', TLSAdapter())
        saml_req_url = prelogin(s, gateway)
        saml_resp_url, saml_resp_data = okta_saml(s, saml_req_url, username, password, factor_priorities, totp_key)
        saml_username, prelogin_cookie = complete_saml(s, saml_resp_url, saml_resp_data)

    subprocess_args = [
        'openconnect',
        gateway,
        '--protocol=gp',
        '--user=' + saml_username,
        '--usergroup=gateway:prelogin-cookie',
        '--passwd-on-stdin'
    ] + list(openconnect_args)

    if sudo:
        subprocess_args = ['sudo'] + subprocess_args

    with popen_forward_sigterm(subprocess_args, stdin=subprocess.PIPE) as p:
        p.stdin.write(prelogin_cookie.encode())
    sys.exit(p.returncode)

main()
