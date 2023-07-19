use {
    futures::stream::TryStreamExt as _,
    tokio::io::{
        self,
        AsyncWrite,
    },
    tokio_util::compat::FuturesAsyncReadCompatExt as _,
    url::Url,
    wheel::traits::IoResultExt as _,
    crate::Error,
};

pub(crate) async fn download(client: &reqwest::Client, url: Url, file: &mut (impl AsyncWrite + Unpin)) -> Result<(), Error> {
    let mut reader = client.get(url)
        .send().await?
        .error_for_status()?
        .bytes_stream()
        //.map_ok(|| )
        .map_err(reqwest_error_to_io)
        .into_async_read()
        .compat();
    tokio::io::copy(
        &mut reader,
        file,
    ).await.at_unknown()?; //TODO annotate?
    Ok(())
}

fn reqwest_error_to_io(e: reqwest::Error) -> io::Error {
    io::Error::new(
        if e.is_timeout() { io::ErrorKind::TimedOut } else { io::ErrorKind::Other }, //TODO other error kinds depending on methods/status?
        Box::new(e)
    )
}
