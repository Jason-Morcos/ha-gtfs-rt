# Home Assistant Realtime GTFS

This project contains a new sensor that provides real-time departure data for
local transit systems that provide gtfs feeds.

## Installation (HACS) - Recommended
0. Have [HACS](https://custom-components.github.io/hacs/installation/manual/) installed, this will allow you to easily update
1. Add `https://github.com/Jason-Morcos/ha-gtfs-rt` as a [custom repository](https://custom-components.github.io/hacs/usage/settings/#add-custom-repositories) as Type: Integration
2. Click install under "GTFS-Realtime", restart your instance.

## Installation (Manual)
1. Download this repository as a ZIP (green button, top right) and unzip the archive
2. Copy `/custom_components/gtfs_rt` to your `<config_dir>/custom_components/` directory
   * You will need to create the `custom_components` folder if it does not exist
   * On Hassio the final location will be `/config/custom_components/gtfs_rt`
   * On Hassbian the final location will be `/home/homeassistant/.homeassistant/custom_components/gtfs_rt`

## Configuration

Add the following to your `configuration.yaml` file:

```yaml
# Example entry for Austin TX

gtfs_rt:
  - name: Austin Metro
    entity_namespace: gtfs_austin
    trip_update_url: 'https://data.texas.gov/download/rmk2-acnw/application%2foctet-stream'
    vehicle_position_url: 'https://data.texas.gov/download/eiei-9rpf/application%2Foctet-stream'
    static_schedule_url: 'https://example.com/google_transit.zip'
    departures:
      - name: Downtown to airport
        unique_id: 3f2f8b2e-8ed2-4d7c-b2a6-9a8f0911b7a9
        route: 100
        stopid: 514
```

```yaml
# Example entry for Seattle WA

gtfs_rt:
  - name: King County Metro
    entity_namespace: gtfs_kingcountymetro
    trip_update_url: 'http://api.pugetsound.onebusaway.org/api/gtfs_realtime/trip-updates-for-agency/1.pb?key=TEST'
    vehicle_position_url: 'http://api.pugetsound.onebusaway.org/api/gtfs_realtime/vehicle-positions-for-agency/1.pb?key=TEST'
    static_schedule_url: 'https://metro.kingcounty.gov/gtfs/google_transit.zip'
    departures:
      - name: "48 to Uni"
        unique_id: 8af3e2dd-9f0a-4b84-8ec0-109c9d2a7c4f
        route: 100228
        stopid: 36800
```

```yaml
# Example entry for Montreal

- platform: gtfs_rt
  trip_update_url: 'https://api.stm.info/pub/od/gtfs-rt/ic/v2/tripUpdates'
  vehicle_position_url: 'https://api.stm.info/pub/od/gtfs-rt/ic/v2/vehiclePositions'
  apikey: <api key>
  departures:
  - name: "Bus 178"
    unique_id: 4b38ec0f-7b3e-47c2-9ee0-1f6a4c1c7f54
    route: 168
    stopid: 56698 
```

```yaml
# Example entry for NYC

- platform: gtfs_rt
    trip_update_url: 'https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm'
    x_api_key: <api key>
    departures:
      - name: "Brooklyn F"
        unique_id: 0b45f0b3-1b02-49c8-96a2-cdcb12183947
        route: 'F'
        stopid: 'F16S'
```

```yaml
# Example entry for Boston

- platform: gtfs_rt
    trip_update_url: 'https://cdn.mbta.com/realtime/TripUpdates.pb'
    vehicle_position_url: 'https://cdn.mbta.com/realtime/VehiclePositions.pb'
    departures:
      - name: "MBTA Red Line Kendall/MIT to Ashmont/Braintree"
        unique_id: 7e8c7d6a-5c9c-46c4-8ab0-2a4b5a1c9a74
        route: 'Red'
        stopid: '70071'
```

Configuration variables:

- **name** (*Optional*): Friendly title for the GTFS feed. Used for the config-entry title and as a prefix for route devices.
- **entity_namespace** (*Optional*): Stable namespace used as the feed identifier during YAML import. Reusing the same value lets existing entities keep their entity IDs after migration.
- **trip_update_url** (*Required*): Provides bus route etas. See the **Finding Feeds** section at the bottom of the page for more details on how to find these
- **vehicle_position_url** (*Optional*): Provides live bus position tracking on the home assistant map
- **static_schedule_url** (*Optional*): A static GTFS ZIP feed used to validate whether a stop is valid and whether service should currently exist. When configured, the entity stays `unknown` during normal no-service windows, and also stays `unknown` when a valid route/stop pair is missing from a truncated realtime stop list. It becomes `unavailable` when the route/stop is invalid or when the realtime feed itself cannot be fetched.
- **headers**(*Optional*): Expects a dictionary. If provided, the dictionary will be sent as headers. (e.g. {"Authorization": "mykey"})
- **departures** (*Required*): A list of routes and departure locations to watch
- **unique_id** (*Optional*): A UUID for the entity to allow entity registry entries
- **route** (*Optional*): The name of the gtfs route
- **stopid** (*Optional*): The stopid for the location you want etas for

When `static_schedule_url` is configured, each sensor also adds:

- `Service status`
- `Service today`
- `Service expected now`
- `Next scheduled departure`
- `Problem reason`

When the feed is configured under the top-level `gtfs_rt:` key, the integration imports it into a Home Assistant config entry. That allows each route to appear as its own service device, so a line like `372` can group all of your chosen stops under a single device.

The legacy `sensor: - platform: gtfs_rt` format still works, but it will not create route devices and is now considered deprecated.

## Screenshot

![screenshot](https://i.imgur.com/VMcX9aG.png)

## Finding Feeds

[Transit Feeds](https://transitfeeds.com) is a fairly good source for realtime
gtfs feeds. Search for your city, and then look for a feed that is tagged with
'GTFS-RealTime'. There should be an 'official url' in the side bar that you can
use. Routes and stops can be found by clicking on the regular gtfs feed, and
finding the id for the stop you are interested in. Please feel free to message
me or open an issue if you find other good sources.

## Reporting an Issue

1. Setup your logger to print debug messages for this component using:
```yaml
logger:
  default: info
  logs:
    custom_components.gtfs_rt: debug
```
2. Restart HA
3. Verify you're still having the issue
4. File an issue in this Github Repository containing your HA log (Developer section > Info > Load Full Home Assistant Log)
   * You can paste your log file at pastebin https://pastebin.com/ and submit a link.
   * Please include details about your setup (Pi, NUC, etc, docker?, HASSOS?)
   * The log file can also be found at `/<config_dir>/home-assistant.log`
