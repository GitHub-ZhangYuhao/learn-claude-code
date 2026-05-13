import os
from dotenv import load_dotenv

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from GlobalConfig import *

load_dotenv()

app = App(token=os.getenv("SLACK_API_TOKEN"))

@app.message('hello')
def hello(message, say):
    say(f'Hello <@{message["user"]}>')

@app.command('/add')
def add(ack, respond, command):
    ack()

    try:
        a, b = map(float, command['text'].split())
        respond(str(a + b))
    except:
        respond("Use: /add 2 3")

@app.event('app_mention')
def mention_gpt_response(event, say):
    text = event.get("text", "")
    response = client.responses.create(
        model = MODEL,
        input = text,
    )
    say(response.output_text)

SocketModeHandler(app, os.getenv('SLACK_SOCKET_TOKEN')).start()