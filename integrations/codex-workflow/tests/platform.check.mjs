import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import http from 'node:http';
import { WebSocketServer } from 'ws';
import { openSocket } from '../desktop-runtime/transport.mjs';
import { connectionTarget } from '../desktop-runtime/endpoint.mjs';
import { listenBridge } from '../desktop-runtime/bridge-transport.mjs';
import { makePrivate } from '../desktop-runtime/platform.mjs';

const close = server => new Promise(resolve => server.close(resolve));
test('TCP transport authenticates, preserves messages, and refuses untrusted endpoint forms', async () => {
 const root = fs.mkdtempSync(path.join(os.tmpdir(),'lm-platform-'));
 makePrivate(root);
 const tokenFile = path.join(root,'token'); fs.writeFileSync(tokenFile,'a'.repeat(64),{mode:0o600});
 const server=http.createServer();const ws=new WebSocketServer({noServer:true});
 const seen=[];
 server.on('upgrade',(req,socket,head)=>{
  seen.push(req.headers.authorization);
  if(req.headers.authorization!=='Bearer '+'a'.repeat(64)){socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');return}
  ws.handleUpgrade(req,socket,head,peer=>{ws.emit('connection',peer,req)});
 });
 ws.on('connection',socket=>socket.on('message',data=>socket.send(data.toString())));
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const endpoint=`ws://127.0.0.1:${server.address().port}`;
 let socket;
 try{
  socket=await openSocket({endpoint,tokenFile});
  const reply=new Promise(resolve=>socket.once('message',data=>resolve(data.toString())));
  socket.send('{"id":1,"method":"example"}');assert.equal(await reply,'{"id":1,"method":"example"}');
  assert.deepEqual(seen,['Bearer '+'a'.repeat(64)]);
  fs.writeFileSync(tokenFile,'b'.repeat(64));
  await assert.rejects(openSocket({endpoint,tokenFile}),/401/);
  for(const value of ['ws://localhost:1234','ws://0.0.0.0:1234','ws://127.0.0.1:65536','ws://127.0.0.1:1234/rpc','ws://user@127.0.0.1:1234','wss://remote:1234']) assert.throws(()=>connectionTarget(value,tokenFile));
  assert.throws(()=>connectionTarget(endpoint));
  if(process.platform!=='win32'){fs.chmodSync(tokenFile,0o644);await assert.rejects(openSocket({endpoint,tokenFile}),/private owned/)}
 }finally{socket?.terminate();for(const peer of ws.clients)peer.terminate();await new Promise(resolve=>ws.close(resolve));await close(server);fs.rmSync(root,{recursive:true,force:true})}
});

test('Windows bridge mechanism rejects an invalid token before invoking any plugin and preserves first request bytes',async()=>{
 let calls=0;
 const bridge=await listenBridge('/unused','codex_app',socket=>{calls++;socket.pipe(socket)},'win32');
 const connect=()=>net.createConnection({host:'127.0.0.1',port:bridge.endpoint.port});
 const bad=connect();bad.on('error',()=>{});
 bad.write('x'.repeat(64)+'\nrequest');await new Promise(resolve=>bad.once('close',resolve));assert.equal(calls,0);
 const good=connect();const reply=new Promise(resolve=>good.once('data',data=>resolve(data.toString())));
 good.write(bridge.endpoint.token.slice(0,12));good.write(bridge.endpoint.token.slice(12)+'\nfirst request\n');
 assert.equal(await reply,'first request\n');assert.equal(calls,1);
 good.destroy();await close(bridge.server);
});

test('Windows activation restores values and registry types, rejects concurrent edits and rolls back failed activation', async()=>{
 const { manageWindows }=await import('../desktop-runtime/manage-windows.mjs');
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'lm-win-activation-'));makePrivate(root);
 const config={configFile:path.join(root,'runtime.json'),launcher:'C:\\fixture\\launch-adapter.exe',helper:'fixture'};
 const before={values:{CODEX_CLI_PATH:'%LOCALAPPDATA%\\original.exe',CODEX_APP_SERVER_FORCE_CLI:null,CODEX_APP_SERVER_WS_URL:null,CODEX_APP_SERVER_USE_LOCAL_DAEMON:null},types:{CODEX_CLI_PATH:2}};
 let state=structuredClone(before), registered=false, busy=false, fail=false;
 const ops={
  native:(operation,req)=>{
   if(operation==='environment-write'){state=structuredClone(req);if(fail){fail=false;throw new Error('write failed')}}
   return structuredClone(state);
  },
  register:()=>{registered=true},unregister:()=>{registered=false},restore:()=>{},running:()=>busy,
 };
 try{
  manageWindows('enable',config,ops);assert.ok(registered);assert.equal(state.types.CODEX_CLI_PATH,1);
  busy=true;assert.throws(()=>manageWindows('disable',config,ops),/Quit/);busy=false;
  state.types.CODEX_CLI_PATH=2;assert.throws(()=>manageWindows('disable',config,ops),/changed externally/);
  state.types.CODEX_CLI_PATH=1;manageWindows('disable',config,ops);assert.deepEqual(state,before);assert.equal(registered,false);
  manageWindows('enable',config,ops);
  const native = ops.native;
  let partial = true;
  ops.native = (operation, request) => {
    if (operation === 'environment-write' && partial) {
      partial = false;
      state.values.CODEX_CLI_PATH = request.values.CODEX_CLI_PATH;
      state.types.CODEX_CLI_PATH = request.types.CODEX_CLI_PATH;
      throw new Error('partial restore');
    }
    return native(operation, request);
  };
  assert.throws(()=>manageWindows('disable',config,ops), /partial restore/);
  assert.equal(registered,true);
  assert.throws(()=>manageWindows('enable',config,ops), /Finish disconnecting/);
  state.values.CODEX_APP_SERVER_WS_URL = 'user-edit';
  assert.throws(()=>manageWindows('disable',config,ops), /changed externally/);
  state.values.CODEX_APP_SERVER_WS_URL = null;
  manageWindows('disable',config,ops);assert.deepEqual(state,before);assert.equal(registered,false);
  fail=true;assert.throws(()=>manageWindows('enable',config,ops),/write failed/);assert.deepEqual(state,before);assert.equal(registered,false);
  assert.equal(fs.existsSync(path.join(root,'activation.json')),false);
 }finally{fs.rmSync(root,{recursive:true,force:true})}
});
