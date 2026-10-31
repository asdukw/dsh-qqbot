import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.asyncio.server import serve


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        print('LLM_REQUEST', self.path, flush=True)
        if body.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for delta, finish in [({'role':'assistant','content':'isolated smoke ok'}, None), ({}, 'stop')]:
                event = {'id':'smoke','object':'chat.completion.chunk','model':'fake','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
                self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'id':'smoke','choices':[{'message':{'role':'assistant','content':'isolated smoke ok'},'finish_reason':'stop'}], 'usage':{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}}).encode())
    def log_message(self, format, *args):
        pass

async def onebot(ws):
    assert ws.request.headers.get('Authorization') == 'Bearer dummy-onebot-token'
    await ws.send(json.dumps({'post_type':'meta_event','meta_event_type':'lifecycle','sub_type':'connect','self_id':10000,'time':1}))
    async for message in ws:
        request = json.loads(message)
        print('ONEBOT_ACTION', request['action'], flush=True)
        data = {'user_id':10000,'nickname':'isolated'} if request['action'] == 'get_login_info' else {'online':True,'good':True}
        await ws.send(json.dumps({'status':'ok','retcode':0,'data':data,'echo':request.get('echo')}))

async def main():
    threading.Thread(target=ThreadingHTTPServer(('0.0.0.0',8765),Handler).serve_forever, daemon=True).start()
    async with serve(onebot,'0.0.0.0',3001):
        print('FAKES_READY',flush=True)
        await asyncio.Future()
asyncio.run(main())
