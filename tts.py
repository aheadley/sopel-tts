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

VALID_AUDIO_FORMATS         = ['mp3', 'ogg_vorbis', 'pcm']
VALID_SAMPLE_RATES          = ['8000', '16000', '22050']
VALID_VOICE_PITCHES         = ['low', 'medium', 'high']
MESSAGE_TEMPLATE            = u"""
<speak><prosody rate="{rate}" pitch="{pitch}">{message}</prosody></speak>
""".strip()
PHONEME_TEMPLATE            = u'<phoneme ph="{}">{}</phoneme>'
EMPHASIS_TEMPLATE           = u'<emphasis level="{}">{}</emphasis>'
TOKEN_REPLACEMENT_MAP       = {
    r'lol':                      'ph:l o l',
    r'\<3':                      'heart',
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

class TTSSection(StaticSection):
    access_key      = ValidatedAttribute('access_key', str)
    secret_key      = ValidatedAttribute('secret_key', str)
    region          = ValidatedAttribute('region', str, default='us-east-1')

    default_lang    = ValidatedAttribute('default_lang',
        lambda lf: len(lf) == 2 and str(lf), default='en')
    force_lang      = ValidatedAttribute('force_lang',
        lambda fl: fl.strip().lower() == 'true', default='false')
    audio_format    = ChoiceAttribute('audio_format', VALID_AUDIO_FORMATS, default='mp3')
    sample_rate     = ChoiceAttribute('sample_rate', VALID_SAMPLE_RATES, default='22050')
    speech_rate     = ValidatedAttribute('speech_rate', float, default=1.1)

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

@sopel.module.rule(r'.*')
def speak(bot, trigger):
    original_msg = trigger.group(0).strip()
    if trigger.nick != bot.nick \
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

    import langid
    # there's a large delay while langid loads the model on the first classify()
    # call, so we do that now
    langid.classify(STARTUP_MESSAGE)

    for old_fn in glob.glob(os.path.join(tempfile.gettempdir(), 'sopel-tts-*.*')):
        log.info('Deleteing old temp file: %s', old_fn)
        os.unlink(old_fn)

    polly_client = boto3.client('polly',
        aws_access_key_id=tts_config.access_key,
        aws_secret_access_key=tts_config.secret_key,
    )

    voices = sorted(polly_client.describe_voices()['Voices'], key=lambda v: v['Id'])
    voice_langs = set(v['LanguageCode'].split('-')[0] for v in voices)
    log.debug(u'Found %d voices from language families: %s',
        len(voices), u', '.join(voice_langs))

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
            msg_lang = langid.classify(orig_msg)[0]
            log.debug(u'Detected message language as: %s', msg_lang)
            if msg_lang not in voice_langs:
                msg_lang = tts_config.default_lang
                log.warning(u'Detected language is not an available voice, defaulting to: %s', msg_lang)
        msg_voices = [v for v in voices if v['LanguageCode'].startswith(msg_lang + '-')]
        voice = nick2bucket(nick, msg_voices)

        log.info(u'Synthesizing with "%s" [%s@%s]: "%s"',
            voice['Id'], tts_config.audio_format, tts_config.sample_rate, msg)
        resp = polly_client.synthesize_speech(
            Text=msg,
            TextType='ssml',
            VoiceId=voice['Id'],
            OutputFormat=tts_config.audio_format,
            SampleRate=tts_config.sample_rate,
        )

        tmp_f, tmp_fn = tempfile.mkstemp(
            suffix='.' + tts_config.audio_format,
            prefix='sopel-tts-',
        )
        os.close(tmp_f)
        log.debug('Writing audio to: %s', tmp_fn)
        with open(tmp_fn, 'w') as tmp_f:
            try:
                tmp_f.write(resp['AudioStream'].read())
            except Exception as err:
                log.error('Failed to write audio file (%s): %s', tmp_fn, err)
                log.exception(err)
            else:
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
