import { test } from 'node:test';
import assert from 'node:assert/strict';
import { multipartPhoto } from './multipart.js';

test('multipart preserves signed fields, Unicode and binary bytes with file strictly last',()=> {
  const photo=new Uint8Array([255,216,0,13,10,128,255,217]);
  const fields={key:'staging/测试.jpg',policy:'abc+/==','x-amz-signature':'signature','x-amz-meta-label':'café'};
  const form=multipartPhoto(fields,photo.buffer,'test-boundary'),body=Buffer.from(form.body);
  assert.equal(form.contentType,'multipart/form-data; boundary=test-boundary');
  let prefix='';for(const [name,value] of Object.entries(fields))prefix+='--test-boundary\r\nContent-Disposition: form-data; name="'+name+'"\r\n\r\n'+value+'\r\n';
  prefix+='--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="photo.jpg"\r\nContent-Type: image/jpeg\r\n\r\n';
  assert.deepEqual(body,Buffer.concat([Buffer.from(prefix),Buffer.from(photo),Buffer.from('\r\n--test-boundary--\r\n')]));
});
test('multipart rejects invalid boundaries, header injection and conflicting file fields',()=> {
  for(const [fields,boundary] of [[{file:'bad'},'safe'],[{'bad\r\nname':'bad'},'safe'],[{policy:4},'safe'],[{},'bad\r\nboundary']])assert.throws(()=>multipartPhoto(fields,new ArrayBuffer(0),boundary));
});
