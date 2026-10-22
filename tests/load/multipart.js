// k6's object encoder iterates Go maps, so field insertion order is not portable.
// S3 requires the file to be the final part. Serialize explicitly on every OS.
function utf8(text) {
  const encoded=encodeURIComponent(text), bytes=[];
  for(let i=0;i<encoded.length;i++) {
    if(encoded[i]==='%') { bytes.push(parseInt(encoded.slice(i+1,i+3),16));i+=2; }
    else bytes.push(encoded.charCodeAt(i));
  }
  return new Uint8Array(bytes);
}
export function multipart(fields,photo,boundary) {
  const chunks=[];
  for(const [name,value] of Object.entries(fields)) {
    if(/[\r\n"\\]/.test(name)) throw Error('Invalid upload field name');
    chunks.push(utf8(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`));
  }
  chunks.push(utf8(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="photo.jpg"\r\nContent-Type: image/jpeg\r\n\r\n`));
  chunks.push(new Uint8Array(photo),utf8(`\r\n--${boundary}--\r\n`));
  const body=new Uint8Array(chunks.reduce((n,b)=>n+b.length,0));let offset=0;
  for(const chunk of chunks) { body.set(chunk,offset);offset+=chunk.length; }
  return body.buffer;
}
