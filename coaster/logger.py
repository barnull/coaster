# -*- coding: utf-8 -*-

"""
Logger
=======

Coaster can help your application log errors at run-time. Initialize with
:func:`coaster.logger.init_app`. If you use :func:`coaster.app.init_app`,
this is done automatically for you.
"""

from __future__ import absolute_import, print_function
from datetime import timedelta, datetime
import logging.handlers
import traceback
import requests
from pprint import pprint
import six
from flask import g, request, session
from .auth import current_auth


# global var as lazy in-memory cache
error_throttle_timestamp_sms = {}
error_throttle_timestamp_slack = {}


def pprint_with_indent(value, outfile, indent=4):
    out = six.StringIO()
    pprint(value, out)
    lines = out.getvalue().split('\n')
    out.close()
    outfile.write('\n'.join([(' ' * indent) + l for l in lines]))


class LocalVarFormatter(logging.Formatter):
    """
    Custom log formatter that logs the contents of local variables in the stack frame.
    """
    def format(self, record):
        """
        Format the specified record as text. Overrides
        :meth:`logging.Formatter.format` to remove cache of
        :attr:`record.exc_text` unless it was produced by this formatter.
        """
        if record.exc_info:
            if record.exc_text:
                if "Stack frames (most recent call first)" not in record.exc_text:
                    record.exc_text = None
        return super(LocalVarFormatter, self).format(record)

    def formatException(self, ei):
        tb = ei[2]
        while True:
            if not tb.tb_next:
                break
            tb = tb.tb_next
        stack = []
        f = tb.tb_frame
        while f:
            stack.append(f)
            f = f.f_back

        sio = six.StringIO()
        traceback.print_exception(ei[0], ei[1], ei[2], None, sio)

        print('\n----------\n', file=sio)
        # XXX: The following text is used as a signature in :meth:`format` above
        print("Stack frames (most recent call first):", file=sio)
        for frame in stack:
            print('\n----\n', file=sio)
            print("Frame %s in %s at line %s" % (frame.f_code.co_name,
                frame.f_code.co_filename,
                frame.f_lineno), file=sio)
            for key, value in list(frame.f_locals.items()):
                print("\t%20s = " % key, end=' ', file=sio)
                try:
                    print(repr(value), file=sio)
                except:  # NOQA
                    # We need a bare except clause because this is the exception handler.
                    # It can't have exceptions of its own.
                    print("<ERROR WHILE PRINTING VALUE>", file=sio)

        if request:
            print('\n----------\n', file=sio)
            print("Request context:", file=sio)
            request_data = {
                'form': request.form,
                'args': request.args,
                'cookies': request.cookies,
                'stream': request.stream,
                'headers': request.headers,
                'data': request.data,
                'files': request.files,
                'environ': request.environ,
                'method': request.method,
                'blueprint': request.blueprint,
                'endpoint': request.endpoint,
                'view_args': request.view_args
            }
            try:
                pprint_with_indent(request_data, sio)
            except:  # NOQA
                print("<ERROR WHILE PRINTING VALUE>", file=sio)

        if session:
            print('\n----------\n', file=sio)
            print("Session cookie contents:", file=sio)
            try:
                pprint_with_indent(session, sio)
            except:  # NOQA
                print("<ERROR WHILE PRINTING VALUE>", file=sio)

        if g:
            print('\n----------\n', file=sio)
            print("App context:", file=sio)
            try:
                pprint_with_indent(vars(g), sio)
            except:  # NOQA
                print("<ERROR WHILE PRINTING VALUE>", file=sio)

        if current_auth:
            print('\n----------\n', file=sio)
            print("Current auth:", file=sio)
            try:
                pprint_with_indent(vars(current_auth), sio)
            except:  # NOQA
                print("<ERROR WHILE PRINTING VALUE>", file=sio)

        s = sio.getvalue()
        sio.close()
        if s[-1:] == '\n':
            s = s[:-1]
        return s


class SMSHandler(logging.Handler):
    """
    Custom logging handler to send SMSes to admins
    """
    def __init__(self, app_name, exotel_sid, exotel_token, exotel_from, twilio_sid, twilio_token, twilio_from, phonenumbers):
            logging.Handler.__init__(self)
            self.app_name = app_name
            self.phonenumbers = phonenumbers
            self.exotel_sid = exotel_sid
            self.exotel_token = exotel_token
            self.exotel_from = exotel_from
            self.twilio_sid = twilio_sid
            self.twilio_token = twilio_token
            self.twilio_from = twilio_from

    def emit(self, record):
        throttle_key = (record.module, record.lineno)
        if throttle_key not in error_throttle_timestamp_sms or (
                (datetime.utcnow() - error_throttle_timestamp_sms[throttle_key]) > timedelta(minutes=5)):
            msg = u"{message}: {info}".format(
                message=record.message,
                info=repr(record.exc_info[1]) if record.exc_info else '')
            for phonenumber in self.phonenumbers:
                self.sendsms(phonenumber,
                    u"Error in {name}. {msg}. Please check your email for details".format(
                        name=self.app_name, msg=msg))
            error_throttle_timestamp_sms[throttle_key] = datetime.utcnow()

    def sendsms(self, number, message):
        try:
            if number.startswith('+91'):  # Indian
                requests.post('https://twilix.exotel.in/v1/Accounts/{sid}/Sms/send.json'.format(sid=self.exotel_sid),
                    auth=(self.exotel_sid, self.exotel_token),
                    data={
                        'From': self.exotel_from,
                        'To': number,
                        'Body': message
                        })
            else:
                requests.post('https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'.format(sid=self.twilio_sid),
                    auth=(self.twilio_sid, self.twilio_token),
                    data={
                        'From': self.twilio_from,
                        'To': number,
                        'Body': message
                        })
        except:  # NOQA
            # We need a bare except clause because this is the exception handler.
            # It can't have exceptions of its own.
            pass


class SlackHandler(logging.Handler):
    """
    Custom logging handler to post error reports to Slack.
    """
    def __init__(self, app_name, webhooks):
        super(SlackHandler, self).__init__()
        self.app_name = app_name
        self.webhooks = webhooks

    def emit(self, record):
        throttle_key = (record.module, record.lineno)
        if throttle_key not in error_throttle_timestamp_slack or (
                (datetime.utcnow() - error_throttle_timestamp_slack[throttle_key]) > timedelta(minutes=5)):

            # Sanity check:
            # If we're not going to be reporting this, don't bother to format the payload
            if record.levelname not in [lname for webhook in self.webhooks for lname in webhook.get('levelnames', [])]:
                return

            if record.exc_text:
                double_split = [s.split('----') for s in record.exc_text.split('----------')]
                flat_list = [item for sublist in double_split for item in sublist]
                # Separate out the first line of each section. It'll be used as the "pretext"
                # while the rest will be used as a "text" attachment.
                sections = [s.strip().split('\n', 1) for s in flat_list]
            else:
                sections = []

            data = {
                'text': u"*{levelname}* in {name}: {message}: `{info}`".format(
                    levelname=record.levelname, name=self.app_name, message=record.message,
                    info=repr(record.exc_info[1]) if record.exc_info else ''),
                'attachments': [{
                    'mrkdwn_in': ['text'],
                    'fallback': section[0],
                    'pretext': section[0],
                    'text': ('```\n' + section[1] + '\n```') if len(section) > 1 else '',
                    } for section in sections]}

            for webhook in self.webhooks:
                if record.levelname not in webhook.get('levelnames', []):
                    continue
                payload = dict(data)
                for attr in ('channel', 'username', 'icon_emoji'):
                    if attr in webhook:
                        payload[attr] = webhook[attr]

                try:
                    requests.post(webhook['url'], json=payload,
                        headers={'Content-Type': 'application/json'})
                except:  # NOQA
                    # We need a bare except clause because this is the exception handler.
                    # It can't have exceptions of its own.
                    pass
                error_throttle_timestamp_slack[throttle_key] = datetime.utcnow()


def init_app(app):
    """
    Enables logging for an app using :class:`LocalVarFormatter`. Requires the
    app to be configured and checks for the following configuration parameters.
    All are optional:

    * ``LOGFILE``: Name of the file to log to (default ``error.log``)
    * ``ADMINS``: List of email addresses of admins who will be mailed error reports
    * ``MAIL_DEFAULT_SENDER``: From address of email. Can be an address or a tuple with name and address
    * ``MAIL_SERVER``: SMTP server to send with (default ``localhost``)
    * ``MAIL_USERNAME`` and ``MAIL_PASSWORD``: SMTP credentials, if required
    * ``SLACK_LOGGING_WEBHOOKS``: If present, will send error logs to all specified Slack webhooks
    * ``ADMIN_NUMBERS``: List of mobile numbers of admin to send SMS alerts. Requires the following values too
    * ``SMS_EXOTEL_SID``: Exotel SID for Indian numbers (+91 prefix)
    * ``SMS_EXOTEL_TOKEN``: Exotel token
    * ``SMS_EXOTEL_FROM``: Exotel sender's number
    * ``SMS_TWILIO_SID``: Twilio SID for non-Indian numbers
    * ``SMS_TWILIO_TOKEN``: Twilio token
    * ``SMS_TWILIO_FROM``: Twilio sender's number

    Format for ``SLACK_LOGGING_WEBHOOKS``::

        SLACK_LOGGING_WEBHOOKS = [{
            'levelnames': ['WARNING', 'ERROR', 'CRITICAL'],
            'url': 'https://hooks.slack.com/...'
            }]

    """
    formatter = LocalVarFormatter()

    error_log_file = app.config.get('LOGFILE', 'error.log')
    if error_log_file:  # Specify a falsy value in config to disable the log file
        file_handler = logging.FileHandler(error_log_file)
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.WARNING)
        app.logger.addHandler(file_handler)

    if app.config.get('ADMIN_NUMBERS'):
        if all(key in app.config for key in ['SMS_EXOTEL_SID', 'SMS_EXOTEL_TOKEN', 'SMS_EXOTEL_FROM',
                'SMS_TWILIO_SID', 'SMS_TWILIO_TOKEN', 'SMS_TWILIO_FROM']):

            # A little trickery because directly creating
            # an SMSHandler object didn't work
            logging.handlers.SMSHandler = SMSHandler

            sms_handler = logging.handlers.SMSHandler(
                app_name=app.name,
                exotel_sid=app.config['SMS_EXOTEL_SID'],
                exotel_token=app.config['SMS_EXOTEL_TOKEN'],
                exotel_from=app.config['SMS_EXOTEL_FROM'],
                twilio_sid=app.config['SMS_TWILIO_SID'],
                twilio_token=app.config['SMS_TWILIO_TOKEN'],
                twilio_from=app.config['SMS_TWILIO_FROM'],
                phonenumbers=app.config['ADMIN_NUMBERS'])
            sms_handler.setLevel(logging.ERROR)
            app.logger.addHandler(sms_handler)

    if app.config.get('SLACK_LOGGING_WEBHOOKS'):
        logging.handlers.SlackHandler = SlackHandler
        slack_handler = logging.handlers.SlackHandler(
            app_name=app.name, webhooks=app.config['SLACK_LOGGING_WEBHOOKS'])
        slack_handler.setFormatter(formatter)
        slack_handler.setLevel(logging.NOTSET)
        app.logger.addHandler(slack_handler)

    if app.config.get('ADMINS'):
        # MAIL_DEFAULT_SENDER is the new setting for default mail sender in Flask-Mail
        # DEFAULT_MAIL_SENDER is the old setting. We look for both
        mail_sender = app.config.get('MAIL_DEFAULT_SENDER') or app.config.get(
            'DEFAULT_MAIL_SENDER', 'logs@example.com')
        if isinstance(mail_sender, (list, tuple)):
            mail_sender = mail_sender[1]  # Get email from (name, email)
        if app.config.get('MAIL_USERNAME') and app.config.get('MAIL_PASSWORD'):
            credentials = (app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'])
        else:
            credentials = None
        mail_handler = logging.handlers.SMTPHandler(app.config.get('MAIL_SERVER', 'localhost'),
            mail_sender,
            app.config['ADMINS'],
            '%s failure' % app.name,
            credentials=credentials)
        mail_handler.setFormatter(formatter)
        mail_handler.setLevel(logging.ERROR)
        app.logger.addHandler(mail_handler)


# Legacy name
configure = init_app
