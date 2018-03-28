#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import hashlib
import tempfile
import subprocess
import os
import os.path
import logging
import urlparse
import re
import multiprocessing
import glob
import time
import functools
from xml.sax.saxutils import escape as xml_escape

import sopel.module
import sopel.tools
import sopel.logger
from sopel.config.types import StaticSection, ValidatedAttribute, ChoiceAttribute, ListAttribute

import boto3
import langid
import googleapiclient.discovery

VALID_VOICE_PITCHES         = ['low', 'medium', 'medium', 'medium', 'high']
MESSAGE_TEMPLATE            = u"""
<speak><prosody rate="{rate}" pitch="{pitch}">{message}</prosody></speak>
""".strip()
PHONEME_TEMPLATE            = u'<phoneme ph="{}">{}</phoneme>'
EMPHASIS_TEMPLATE           = u'<emphasis level="{}">{}</emphasis>'
TOKEN_REPLACEMENT_MAP       = {
    r'lol':                      'ph:l o l',
    r'\<3':                      'heart',
    r'\\o/':                     'hurray',
}
# https://www.youtube.com/watch?v=QrWAdq8e_uA
STARTUP_MESSAGE             = u'Reactor online. Sensors online. Weapons online. All systems nominal.'

log = sopel.logger.get_logger('tts')
log.setLevel(logging.DEBUG)

def multiprocessify(func):
    @functools.wraps(func)
    def wrapper(*pargs, **kwargs):
        return multiprocessing.Process(target=func, args=pargs, kwargs=kwargs)
    return wrapper

def getWorkerLogger(worker_name, level=logging.DEBUG):
    logging.basicConfig()
    log = logging.getLogger('sopel.modules.tts.{}-{:05d}'.format(worker_name, os.getpid()))
    log.setLevel(level)
    return log

validate_bool = lambda v: v.strip().lower() == 'true'
class TTSSection(StaticSection):
    aws_access_key  = ValidatedAttribute('aws_access_key', str)
    aws_secret_key  = ValidatedAttribute('aws_secret_key', str)
    gcloud_api_key  = ValidatedAttribute('gcloud_api_key', str)

    prefer_wavenet  = ValidatedAttribute('prefer_wavenet',
        validate_bool, default='false')
    default_lang    = ValidatedAttribute('default_lang',
        lambda lf: len(lf) == 2 and str(lf), default='en')
    force_lang      = ValidatedAttribute('force_lang',
        validate_bool, default='false')
    confidence_threshold = ValidatedAttribute('confidence_threshold', float, default=0.4)
    speech_rate     = ValidatedAttribute('speech_rate', str, default='default')

    play_cmd        = ValidatedAttribute('play_cmd',
        lambda pc: ('{}' in str(pc) and str(pc)))

    # never speak messages from these nicks
    mute_nicks      = ListAttribute('mute_nicks', default=['ChanServ', 'NickServ'])
    # never speak messages starting with these strings (think bot commands)
    mute_msgs       = ListAttribute('mute_msgs', default=['.', '!'])
    # never speak messages from these channels
    mute_channels   = ListAttribute('mute_channels', default=[])

    startup_msg     = ValidatedAttribute('startup_msg', str, default=STARTUP_MESSAGE)

def setup(bot):
    bot.config.define_section('tts', TTSSection)

    if not bot.memory.contains('tts'):
        bot.memory['tts'] = sopel.tools.SopelMemory()

    # also ignore PMs from muted nicks
    bot.config.tts.mute_channels += bot.config.tts.mute_nicks

    bot.memory['tts']['control'] = {
        'mute':         False,
    }
    bot.memory['tts']['queues'] = {
        'text':         multiprocessing.Queue(),
        'audio':        multiprocessing.Queue(),
    }
    bot.memory['tts']['workers'] = {
        'processor':    handle_messages(bot.memory['tts']['queues']['text'],
                            bot.memory['tts']['queues']['audio'], bot.config.tts),
        'player':       play_audio(bot.memory['tts']['queues']['audio'],
                            bot.config.tts.play_cmd),
    }

    log.debug('Setup finished, starting workers and sending debug message')
    for worker in bot.memory['tts']['workers'].values():
        worker.start()

    bot.memory['tts']['queues']['text'].put((bot.config.tts.startup_msg, bot.nick))

@sopel.module.commands('tts')
@sopel.module.require_owner()
def control(bot, trigger):
    cmd = trigger.group(2).strip().lower()
    if cmd == 'mute':
        bot.memory['tts']['control']['mute'] = True
        log.info(u'Muting tts speech, no new messages will be spoken')
    if cmd == 'unmute':
        bot.memory['tts']['control']['mute'] = False
        log.info(u'Unmuting tts speech, messages will be spoken again')
control.priority = 'medium'

@sopel.module.rule(r'.*')
def speak(bot, trigger):
    original_msg = trigger.group(0).strip()
    if trigger.nick != bot.nick \
            and not bot.memory['tts']['control']['mute'] \
            and str(trigger.nick) not in bot.config.tts.mute_nicks \
            and not any(original_msg.startswith(c) for c in bot.config.tts.mute_msgs) \
            and not any(trigger.sender == c for c in bot.config.tts.mute_channels):
        bot.memory['tts']['queues']['text'].put((original_msg, trigger.nick))
        log.debug(u'Queued message from [%s@%s]: "%s"', trigger.nick, trigger.sender, original_msg)
    else:
        log.info(u'Ignoring message from: %s@%s', trigger.nick, trigger.sender)
speak.priority = 'medium'

@multiprocessify
def handle_messages(msg_queue, audio_queue, tts_config):
    log = getWorkerLogger('processor')
    log.debug('Worker process starting')

    helper = TTSHelper(log, tts_config)
    helper.purge_tmp_files()

    voices = helper.get_all_voices()
    voice_langs = set(v.lang_id.split('-')[0] for v in voices)
    log.debug(u'Found %d voices from language families: %s',
        len(voices), u', '.join(voice_langs))

    lang_classifier = langid.langid.LanguageIdentifier.from_modelstring(
        langid.langid.model, norm_probs=True)
    lang_classifier.set_languages(voice_langs)
    # there's a large delay while langid loads the model on the first classify()
    # call, so we do that now
    lang_classifier.classify(STARTUP_MESSAGE)

    log.info('Ready to work')
    while True:
        orig_msg, nick = msg_queue.get()

        msg = clean_message(orig_msg)
        if not msg:
            log.warning(u'Skipping garbage message: "%s"', msg)
            continue
        msg = MESSAGE_TEMPLATE.format(
            rate=tts_config.speech_rate,
            pitch=nick2bucket(nick, VALID_VOICE_PITCHES),
            message=msg,
        )

        if tts_config.force_lang:
            msg_lang = tts_config.default_lang
            log.debug(u'Forcing message language to: %s', msg_lang)
        else:
            msg_lang, prob = lang_classifier.classify(orig_msg)
            log.debug(u'Detected message language as: %s (p%0.5f)', msg_lang, prob)
            if prob < tts_config.confidence_threshold:
                msg_lang = tts_config.default_lang
                log.info(u'Detection confidence too low (<p%0.2f), defaulting to: %s',
                    tts_config.confidence_threshold, msg_lang)
            if msg_lang not in voice_langs:
                msg_lang = tts_config.default_lang
                log.warning(u'Detected language is not an available voice, defaulting to: %s', msg_lang)
        msg_voices = [v for v in voices if v.lang_id.startswith(msg_lang + '-')]
        if tts_config.prefer_wavenet and any(u'Wavenet' in v.name for v in voices):
            # only use wavenet voices if available
            msg_voices = [v for v in voices if u'Wavenet' in v.name]
        voice = nick2bucket(nick, msg_voices)

        log.info(u'Synthesizing with voice [%s:%s]: "%s"',
            voice.service, voice.name, msg)

        try:
            speech = voice.speak(msg)
        except Exception as err:
            log.error('Failed to synthesize speech')
            log.exception(err)
            continue

        tmp_fn = helper.write_to_tmp_file(speech)
        if tmp_fn is not None:
            audio_queue.put(tmp_fn)
            log.debug('Queued audio file: %s', tmp_fn)
    log.debug('Worker process stopping')

@multiprocessify
def play_audio(audio_queue, play_cmd):
    log = getWorkerLogger('player')
    log.debug('Worker process starting')

    with open('/dev/null', 'rw') as dev_null:
        log.info('Ready to work')
        while True:
            fn = audio_queue.get()
            log.debug('Playing audio file: %s', fn)
            try:
                subprocess.call(play_cmd.format(fn).split(), stdout=dev_null, stderr=dev_null)
            except Exception as err:
                log.error('Failed to play audio file (%s): %s', fn, err)
                log.exception(err)
            else:
                os.unlink(fn)
                log.debug('Deleted audio file: %s', fn)
                time.sleep(0.3)
    log.debug('Worker process stopping')


def nick2bucket(nick, buckets):
    return buckets[int(nick.strip().lower().encode('hex'), 16) % len(buckets)]

def clean_message(msg):
    return u' '.join(clean_token(t) for t in msg.split()).strip()

def clean_token(t):
    url = urlparse.urlparse(t)
    if url.scheme.startswith('http'):
        try:
            domain_parts = url.netloc.split('.')
            # if first part of the domain is 1-3 homogeneous characters, drop it
            # catches things like (i.)imgur.com or (www.)example.com while skipping
            # something like bbc.uk
            if 3 >= len(domain_parts[0]) and 1 == len(set(domain_parts[0])):
                t = u'.'.join(domain_parts[1:])
            else:
                t = url.netloc
            return EMPHASIS_TEMPLATE.format('strong', t)
        except Exception:
            return u''
    # fixup tokens that polly doesn't know how to pronounce
    for rgx, ph in TOKEN_REPLACEMENT_MAP.iteritems():
        if re.match(rgx, t, re.I):
            if ph is None:
                return u''
            elif ph.startswith('ph:'):
                return PHONEME_TEMPLATE.format(ph[3:], t)
            else:
                return ph
    # reduce characters repeated 4+ times in a row to 2 characters
    t = re.sub(r'((.)\2)(\2{2,})', r'\1', t)
    t = xml_escape(t.strip())
    if t.startswith('*') and t.endswith('*') and len(t) >= 3:
        t = EMPHASIS_TEMPLATE.format('strong', t[1:-1])
    return t

class AbstractVoice(object):
    def __init__(self, client, data):
        self._client = client
        self._data = data

    @property
    def name(self): return self._get_name()

    @property
    def gender(self): return self._get_gender()

    @property
    def lang_id(self): return self._get_lang_code()

    def speak(self, message, rate=1.0, format=None):
        raise NotImplemented

class GoogleVoice(AbstractVoice):
    @property
    def service(self):
        return 'google-cloud'

    def _get_name(self):
        return self._data[u'name']

    def _get_gender(self):
        return self._data[u'ssmlGender'].upper()

    def _get_lang_code(self):
        return self._data[u'languageCodes'][0]

    def speak(self, message):
        return self._client.text().synthesize(body={
            'audioConfig': {'audioEncoding': 'MP3', 'pitch': 0.0, 'speakingRate': 1.0},
            'input': {'ssml': message},
            'voice': {'name': self.name, 'languageCode': self.lang_id}}
        ).execute()[u'audioContent'].decode('base64')

class AmazonVoice(AbstractVoice):
    @property
    def service(self):
        return 'aws'

    def _get_name(self):
        return self._data[u'Id']

    def _get_gender(self):
        return self._data[u'Gender'].upper()

    def _get_lang_code(self):
        return self._data[u'LanguageCode']

    def speak(self, message):
        return self._client.synthesize_speech(
            Text=message,
            TextType='ssml',
            VoiceId=self.name,
            OutputFormat='mp3',
            SampleRate='22050',
        )['AudioStream'].read()

class TTSHelper(object):
    def __init__(self, logger, tts_config):
        self.log = logger
        self._config = tts_config
        self._svc_glcoud = googleapiclient.discovery.build(
            'texttospeech', 'v1beta1', developerKey=tts_config.gcloud_api_key)
        self._svc_aws = boto3.client('polly',
            aws_access_key_id=tts_config.aws_access_key,
            aws_secret_access_key=tts_config.aws_secret_key,
        )

    def purge_tmp_files(self):
        for old_fn in glob.glob(os.path.join(tempfile.gettempdir(), 'sopel-tts-*.*')):
            self.log.info('Deleteing old temp file: %s', old_fn)
            os.unlink(old_fn)

    def get_all_voices(self):
        return sorted(self._get_gcloud_voices() + self._get_aws_voices(), key=lambda v: v.name)

    def write_to_tmp_file(self, data):
        tmp_f, tmp_fn = tempfile.mkstemp(
            suffix='.mp3',
            prefix='sopel-tts-',
        )
        os.close(tmp_f)
        self.log.debug('Writing audio to: %s', tmp_fn)
        with open(tmp_fn, 'w') as tmp_f:
            try:
                tmp_f.write(data)
            except Exception as err:
                self.log.error('Failed to write audio file (%s): %s', tmp_fn, err)
                self.log.exception(err)
            else:
                return tmp_fn
        return None

    def _get_gcloud_voices(self):
        return [GoogleVoice(self._svc_glcoud, v) \
            for v in self._svc_glcoud.voices().list().execute()['voices']]

    def _get_aws_voices(self):
        return [AmazonVoice(self._svc_aws, v) \
            for v in self._svc_aws.describe_voices()['Voices']]
