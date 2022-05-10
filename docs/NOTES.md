The Goal
--------

I want something that shows the next 3 buses arriving
at my local bus stop, in either direction (so, 6 lines).

Each line shows:

```
  NN  3:27 0:00 to Destination (Words Words Words) *!
   \   \    \        \             \               |
    \   \    \     headsign       current stop     |
     \   \    \                                    /
      \   \   countdown if < 10 minutes           /
       \  predicted time of departure            / 
        \                                       /
      route number                 various indicators
                                   for alerts, stale
                                    predictions, etc.
                                    
```

Connecting Data
---------------

Some observational notes on connecting data from the
different entities:

* Departure time is generally the most interesting.
  Missing that is what makes you miss your bus.
* It's useful to combine vehicle information with
  prediction information to get more frequent,
  meaningful updates.
* It'd be nice to show time since the most recent
  update to a prediction _or_ the most recent
  update to the associated vehicle.
* To calculate the number of stops away, you need:
  * the `stop_sequence` value from the prediction
  * the `trip_id` from the prediction
  * the `route_id` from the prediction
  * the `vehicle_id` from the prediction
    - which may be None, if the vehicle is assigned
      but the trip hasn't started
  * the `trip_id` from the _vehicle_ — if it doesn't
    match the one from the prediction, that vehicle
    is still on a previous trip
  * assuming they match, compare the `stop_sequence`
    from the _prediction_ to the `current_stop_sequence`
    of the _vehicle_
      - bonus fun: for buses, this is generally a
        counting sequence, but for trains, it can be
        big numbers with big jumps. The only promise is
        that the sequence is monotonically increasing (it
        always goes up — or down).
      - so instead of subtracting, look at the `stops_list`
        in the _trip_ and compare _indices_.
    * speaking of direction, that will be `1` or `0` in
      the _vehicle_ information. You need the _route_ to
      know which number is `Inbound` and which is
      `Outbound` for a given route.
  * while you're doing all that, might as well get
    the name of the vehicle's `current_stop_sequence`...
    remembering that it might not currently be using
    the _predicted_ trip.
  * `headsign`, the label for the front of the bus
    (about where it's going, not special messages)
    is found in the _trip_


Mapping in Home Assistant?
-------------------------

It'd be nice if the buses could just magically show up
on the map, but Home Assistant is more restrictive than
that. The built-in map card only handles named entities
rather than static ones, except for those from
'geolocation' data sources, which this isn't.

Need to make a custom map card, maybe, or ...
something else. One hacky implementation would be
to make template sensors (fixed entities, in other
words) that show the most recent predictions.

Also, it would be nice to decode the polyline strings
in the shape resources — they're not very useful in 
their encoded form! That could be used to draw routes
on the map.

Random Ideas
------------

* Various entity icons really should change based
  on vehicle type. However, this is hard, since
  the _vehicle_ resource doesn't include the type!
  You have to correlate with the route the vehicle
  is on. Or we could just do it based on the pattern
  of the vehicle IDs, which make it obvious.

* It'd be nice to be able to set the stop watch
  list by latitude, longitude, radius. We have
  (or can fetch!) all of the information needed
  to do that.
