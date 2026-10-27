// The viewer-request CloudFront Function (cloudfront-js-2.0), kept as source here so the tests can run it.
//
// The distribution answers for rwbookclub.com and www.rwbookclub.com. Any www. host is a 301 to the
// apex, keeping the path and the query string as the viewer sent them.
//
// S3 behind Origin Access Control serves no directory indexes, so "/books/" is rewritten to its
// index.html and "/books" is sent to the slash form, as GitHub Pages did. A path whose last segment has
// a dot is a file and passes through. The build writes no redirects, so there is no redirect store.
export const DOMAIN = 'rwbookclub.com';

export const viewerRequestCode = `var APEX = 'https://${DOMAIN}';

function search(querystring) {
  var parts = [];
  for (var name in querystring) {
    var item = querystring[name];
    var values = item.multiValue ? item.multiValue : [item];
    for (var i = 0; i < values.length; i++) parts.push(values[i].value === '' ? name : name + '=' + values[i].value);
  }
  return parts.length ? '?' + parts.join('&') : '';
}

function moved(location) {
  return { statusCode: 301, statusDescription: 'Moved Permanently', headers: { location: { value: location } } };
}

function handler(event) {
  var request = event.request;
  var uri = request.uri;
  var host = request.headers && request.headers.host ? request.headers.host.value.toLowerCase() : '';
  if (host.indexOf('www.') === 0) return moved(APEX + uri + search(request.querystring));
  var bare = !uri.endsWith('/') && uri.split('/').pop().indexOf('.') === -1;
  if (bare) return moved(uri + '/' + search(request.querystring));
  if (uri.endsWith('/')) request.uri = uri + 'index.html';
  return request;
}
`;
