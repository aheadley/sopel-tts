# TTS Plugin for Sopel IRC bot
## About

This is a fun little plugin to get TTS (text-to-speech) in IRC. I wrote it to play 
around with AWS's new [Polly TTS](https://aws.amazon.com/polly/) API.

## Installation

~~~~bash
$ git clone https://gist.github.com/497ed96a95960c192d28a978d4cb739d.git ~/devel/sopel-tts
$ virtualenv ~/devel/sopel-tts.env
$ . ~/devel/sopel-tts.env/bin/activate
$ pip install -r ~/devel/sopel-tts/requirements.txt
$ sopel
# configure the IRC network or bouncer/proxy and channel(s) you want the bot to connect to
# then Ctrl+c to kill it
$ ln -s ~/devel/sopel-tts/tts.py ~/.sopel/modules/tts.py
# edit ~/.sopel/default.cfg and set the `[tts]` config options, see example.cfg
$ sopel
# you should hear "All systems nominal, ready for messages." if everything worked correctly
~~~~