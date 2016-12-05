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

    # never speak messages from these nicks
    mute_nicks      = ListAttribute('mute_nicks', default=['ChanServ', 'NickServ'])
    # never speak messages starting with these strings (think bot commands)
    mute_msgs       = ListAttribute('mute_msgs', default=['.', '!'])

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
    log.debug(u'Pulled %d voices: %s', len(voices), u', '.join(v['Id'] for v in voices))

    if not bot.memory.contains('tts'):
        bot.memory['tts'] = sopel.tools.SopelMemory()

    bot.memory['tts']['polly_client'] = polly_client
    bot.memory['tts']['voices'] = voices
    bot.memory['tts']['speech_queue'] = multiprocessing.Queue()
    bot.memory['tts']['speech_proc'] = multiprocessing.Process(target=worker_proc,
        args=(bot.memory['tts']['speech_queue'], polly_client, bot.config.tts))

    log.debug('Setup finished, starting audio worker and queueing debug message')
    bot.memory['tts']['speech_proc'].start()
    bot.memory['tts']['speech_queue'].put((
        'All systems nominal, ready for messages.', nick2bucket(bot.nick, voices)))

@sopel.module.rule(r'.*')
def speak(bot, trigger):
    if trigger.nick != bot.nick and str(trigger.nick) not in bot.config.tts.mute_nicks:
        msg = clean_message(trigger.group(0))
        if msg and not any(msg.startswith(c) for c in bot.config.tts.mute_msgs):
            bot.memory['tts']['speech_queue'].put((
                msg, nick2bucket(trigger.nick, bot.memory['tts']['voices'])))
            log.debug(u'Queued message: "%s"', msg)
        else:
            log.warning(u'Skipping garbage message: "%s"', trigger.group(0))
    else:
        log.info(u'Ignoring muted nick: %s', trigger.nick)
speak.priority = 'medium'

@sopel.module.commands('myvoice')
def show_my_voice(bot, trigger):
    v = nick2bucket(trigger.nick, bot.memory['tts']['voices'])
    bot.say(u'Your voice is: {name} ({lang_name} - {gender}) [{lang_code}]'.format(
        name=v['Id'],
        lang_name=v['LanguageName'],
        gender=v['Gender'],
        lang_code=v['LanguageCode'],
    ))
show_my_voice.priority = 'medium'

def worker_proc(queue, polly_client, tts_config):
    logging.basicConfig()
    log = logging.getLogger('sopel.modules.tts.worker-{:05d}'.format(os.getpid()))
    log.setLevel(logging.DEBUG)

    log.debug('Speech worker process starting...')

    for old_fn in glob.glob(os.path.join(tempfile.gettempdir(), 'sopel-tts-*.*')):
        log.info('Deleteing old temp file: %s', old_fn)
        os.unlink(old_fn)

    keep_running = True
    with open('/dev/null', 'rw') as dev_null:
        log.info('Ready to accept messages!')
        while keep_running:
            msg, voice = queue.get()

            log.info(u'Synthesizing with "%s" [%s@%s]: "%s"',
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
            log.debug('Writing audio to: %s', tmp_fn)
            with open(tmp_fn, 'w') as tmp_f:
                try:
                    tmp_f.write(resp['AudioStream'].read())
                except Exception as err:
                    log.error('Failed to speak: %s', err)
                    log.exception(err)
                    continue
            log.debug('Playing queued audio from: %s', tmp_fn)
            subprocess.call(tts_config.play_cmd.format(tmp_fn).split(), stdout=dev_null, stderr=dev_null)
            os.unlink(tmp_fn)
    log.debug('Speech worker process exiting!')

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
            return u'{{ {} }}'.format(t)
        except Exception:
            return u''
    # reduce characters repeated 4+ times in a row to 2 characters
    t = re.sub(r'((.)\2)(\2{2,})', r'\1', t)
    if not t.encode('ascii', 'ignore').strip():
        # filter out unicode garbage, unfortunately this sucks for other languages
        t = u''
    return t.strip()
