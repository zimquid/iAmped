# TODO

- Hardware-validate the new paths on the 1G nano: autosync-on-connect ritual
  (readback → delta refresh → profile sync), podcast sync, and On-The-Go
  import. All are covered by unit tests but haven't touched a real device yet.
- iPod Podcasts menu: episodes currently sync as regular audio (genre
  "Podcast", per-show playlist) so they play on every generation. Writing the
  native podcast mediatype/flags would move them into the Podcasts menu with
  resume-position support on 4G+ devices.
- MTP autosync: the connect watcher only handles mounted (iPod/USB) devices;
  MTP players stay manual because scanning them seizes the device.
