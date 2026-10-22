// Encode the multipart body explicitly: k6's object-to-form encoder uses a Go
// map and does not preserve part order. S3 requires the file part to be last.
function utf8(text) {
  const encoded=encodeURIComponent(text), bytes=[];
  for(let i=0;i<encoded.length;i++) {
    if(encoded[i]==='%') { bytes.push(parseInt(encoded.slice(i+1,i+3),16)); i+=2; }
    else bytes.push(encoded.charCodeAt(i));
  }
  return new Uint8Array(bytes);
}
export function multipartPhoto(fields,photo,boundary) {
  if(!/^[A-Za-z0-9_-]{1,70}$/.test(boundary)) throw Error('Invalid multipart boundary');
  const chunks=[];
  for(const [name,value] of Object.entries(fields)) {
    if(name==='file' || /[\r\n]/.test(name) || typeof value!=='string') throw Error('Invalid presigned form field');
    const quoted=name.replaceAll('\\','\\\\').replaceAll('"','\\"');
    chunks.push(utf8(`--${boundary}\r\nContent-Disposition: form-data; name="${quoted}"\r\n\r\n${value}\r\n`));
  }
  chunks.push(utf8(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="photo.jpg"\r\nContent-Type: image/jpeg\r\n\r\n`));
  chunks.push(new Uint8Array(photo));
  chunks.push(utf8(`\r\n--${boundary}--\r\n`));
  const bytes=new Uint8Array(chunks.reduce((sum,part)=>sum+part.byteLength,0));
  let offset=0;for(const chunk of chunks) {bytes.set(chunk,offset);offset+=chunk.byteLength;}
  return {body:bytes.buffer,contentType:'multipart/form-data; boundary='+boundary};
}
