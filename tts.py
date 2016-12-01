#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import hashlib
import tempfile
import subprocess
import os
import logging
import urlparse
import re
import multiprocessing

import sopel.module
import sopel.tools
import sopel.logger
from sopel.config.types import StaticSection, ValidatedAttribute, ChoiceAttribute, ListAttribute

import boto3

VALID_AUDIO_FORMATS         = ['mp3', 'ogg_vorbis', 'pcm']
VALID_SAMPLE_RATES          = ['8000', '16000', '22050']

log = sopel.logger.get_logger('tts')
log.setLevel(logging.DEBUG)

class TTSSection(StaticSection):
    access_key      = ValidatedAttribute('access_key', str)
    secret_key      = ValidatedAttribute('secret_key', str)
    region          = ValidatedAttribute('region', str)

    lang_family     = ValidatedAttribute('lang_family',
        lambda lf: len(lf) == 2 and str(lf))
    audio_format    = ChoiceAttribute('audio_format', VALID_AUDIO_FORMATS)
    sample_rate     = ChoiceAttribute('sample_rate', VALID_SAMPLE_RATES)

    play_cmd        = ValidatedAttribute('play_cmd',
        lambda pc: ('{}' in str(pc) and str(pc)))

    mute            = ListAttribute('mute')

def setup(bot):
    bot.config.define_section('tts', TTSSection)
    polly_client = boto3.client('polly',
        aws_access_key_id=bot.config.tts.access_key,
        aws_secret_access_key=bot.config.tts.secret_key,
    )
    voices = sorted([v \
        for v in polly_client.describe_voices()['Voices'] \
        if v['LanguageCode'].startswith(bot.config.tts.lang_family + u'-')],
        key=lambda v: v['Id'])
    log.debug('Pulled %d voices: %s', len(voices), u', '.join(v['Id'] for v in voices))

    if not bot.memory.contains('tts'):
        bot.memory['tts'] = sopel.tools.SopelMemory()

    bot.memory['tts']['polly_client'] = polly_client
    bot.memory['tts']['voices'] = voices
    bot.memory['tts']['speech_queue'] = multiprocessing.Queue()
    bot.memory['tts']['speech_proc'] = multiprocessing.Process(target=worker_proc,
        args=(bot.memory['tts']['speech_queue'], log, polly_client, bot.config.tts))

    bot.memory['tts']['speech_proc'].start()
    bot.memory['tts']['speech_queue'].put((
        'All systems nominal, ready for messages.', nick2bucket(bot.nick, voices)))

@sopel.module.rule(r'.*')
def speak(bot, trigger):
    if trigger.nick != bot.nick and str(trigger.nick) not in bot.config.tts.mute:
        log.info('Speaking for: %s', trigger.nick)

        msg = clean_message(trigger.group(0))
        bot.memory['tts']['speech_queue'].put((
            msg, nick2bucket(trigger.nick, bot.memory['tts']['voices'])))
        log.debug('Queued message: %s', msg)
speak.priority = 'medium'

@sopel.module.commands('myvoice')
def show_my_voice(bot, trigger):
    v = nick2bucket(trigger.nick, bot.memory['tts']['voices'])
    bot.say('Your voice is: {name} ({lang_name} - {gender}) [{lang_code}]'.format(
        name=v['Id'],
        lang_name=v['LanguageName'],
        gender=v['Gender'],
        lang_code=v['LanguageCode'],
    ))
show_my_voice.priority = 'medium'

def worker_proc(queue, log, polly_client, tts_config):
    log.debug('Speech process starting...')
    keep_running = True
    with open('/dev/null', 'rw') as dev_null:
        while keep_running:
            msg, voice = queue.get()

            log.info('Synthesizing with "%s" [%s@%s]: %s',
                voice['Id'], tts_config.audio_format, tts_config.sample_rate, msg)
            resp = polly_client.synthesize_speech(
                Text=msg,
                VoiceId=voice['Id'],
                OutputFormat=tts_config.audio_format,
                SampleRate=tts_config.sample_rate,
            )

            tmp_f, tmp_fn = tempfile.mkstemp(
                suffix='.' + tts_config.audio_format,
                prefix='sopel-tts-',
            )
            os.close(tmp_f)
            log.info('Writing audio to: %s', tmp_fn)
            with open(tmp_fn, 'w') as tmp_f:
                try:
                    tmp_f.write(resp['AudioStream'].read())
                except Exception as err:
                    log.error('Failed to speak: %s', err)
                    log.exception(err)
                    return
            log.info('Playing queued audio from: %s', tmp_fn)
            subprocess.call(tts_config.play_cmd.format(tmp_fn).split(), stdout=dev_null, stderr=dev_null)
            os.unlink(tmp_fn)

def nick2bucket(nick, buckets):
    return buckets[sum(bytearray(hashlib.md5(nick.strip().lower()).digest())) % len(buckets)]

def clean_message(msg):
    return ' '.join(clean_token(t) for t in msg.split())

def clean_token(t):
    url = urlparse.urlparse(t)
    if url.scheme.startswith('http'):
        return url.netloc
    t = re.sub(r'((.)\2{2,})', r'\2', t)
    return t
