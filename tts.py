#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import hashlib
import tempfile
import subprocess
import os
import logging

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
    voices = [v['Id'] \
        for v in polly_client.describe_voices()['Voices'] \
        if v['LanguageCode'].startswith(bot.config.tts.lang_family + u'-')]
    log.debug('Pulled %d voices: %s', len(voices), u', '.join(voices))

    if not bot.memory.contains('tts'):
        bot.memory['tts'] = sopel.tools.SopelMemory()

    bot.memory['tts']['polly_client'] = polly_client
    bot.memory['tts']['voices'] = voices

@sopel.module.rule(r'.*')
def speak(bot, trigger):
    if trigger.nick != bot.nick and str(trigger.nick) not in bot.config.tts.mute:
        log.info('Speaking for: %s', trigger.nick)
        c = bot.memory['tts']['polly_client']
        v = nick2bucket(trigger.nick, bot.memory['tts']['voices'])
        r = c.synthesize_speech(
            Text=trigger.group(0),
            VoiceId=v,
            OutputFormat=bot.config.tts.audio_format,
            SampleRate=bot.config.tts.sample_rate,
        )
        tmp_f, tmp_fn = tempfile.mkstemp()
        os.close(tmp_f)
        log.info('Writing audio to: %s', tmp_fn)
        with open(tmp_fn, 'w') as tmp_f:
            try:
                tmp_f.write(r['AudioStream'].read())
            except Exception as err:
                log.error('Failed to speak: %s', err)
                return
        with open('/dev/null', 'rw') as out:
            log.info('Playing audio from: %s', tmp_fn)
            subprocess.call(bot.config.tts.play_cmd.format(tmp_fn).split(), stdout=out, stderr=out)
        os.unlink(tmp_fn)
speak.priority = 'medium'

@sopel.module.commands('myvoice')
def show_my_voice(bot, trigger):
    v = nick2bucket(trigger.nick, bot.memory['tts']['voices'])
    bot.say('Your voice is: {}'.format(v))

def nick2bucket(nick, buckets):
    return buckets[sum(bytearray(hashlib.md5(nick.strip().lower()).digest())) % len(buckets)]
