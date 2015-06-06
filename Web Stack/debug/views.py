# Create your views here.
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from microdata.models import Event, Device, Appliance, Circuit, CircuitType
from django import forms
from django.shortcuts import render, render_to_response
from django.forms import ModelChoiceField
from django.core import serializers
from django.conf import settings

from influxdb import client as influxdb
from influxdb import SeriesHelper

import git
import random
import json
import numpy
import re
import time
from datetime import datetime

from debug.models import TestEvent
from rest_framework import viewsets
from debug.serializers import TestEventSerializer
from webapp.models import Tier

class EventSeriesHelper(SeriesHelper):
    # Meta class stores time series helper configuration.
    class Meta:
        # The client should be an instance of InfluxDBClient.
        client = influxdb.InfluxDBClient(settings.INFLUXDB_URI, 8086, "root", "root", "seads")
        # The series name must be a string. Add dependent fields/tags in curly brackets.
        series_name = 'device.{serial}'
        tags = ['serial', 'circuit_pk']
        precision = "s"
        # Defines all the fields in this time series.
        fields = ['time', 'wattage', 'cost']
        # Defines the number of data points to store prior to writing on the wire.
        bulk_size = 100000
        # autocommit must be set to True when using bulk_size
        autocommit = True
        
class TierSeriesHelper(SeriesHelper):
    # Meta class stores time series helper configuration.
    class Meta:
        # The client should be an instance of InfluxDBClient.
        client = influxdb.InfluxDBClient(settings.INFLUXDB_URI, 8086, "root", "root", "seads")
        # The series name must be a string. Add dependent fields/tags in curly brackets.
        series_name = 'tier.device.{serial}'
        tags = ['serial']
        precision = "s"
        # Defines all the fields in this time series.
        fields = ['time', 'level']
        # autocommit must be set to True when using bulk_size
        autocommit = True

class TestEventViewSet(viewsets.ModelViewSet):
    queryset = TestEvent.objects.all()
    serializer_class = TestEventSerializer

class DeviceModelChoiceField(ModelChoiceField):
   def label_from_instance(self, obj):
      return "%s (%i)" % (obj, obj.serial)

class DatagenForm(forms.Form):
   device = DeviceModelChoiceField(label='Device', queryset=Device.objects.all())
   channels = forms.ModelMultipleChoiceField(label='Channels', queryset=CircuitType.objects.all())
   start = forms.IntegerField(label='Start (10-digit timestamp) UTC')
   stop = forms.IntegerField(label='Stop (10-digit timestamp) UTC')
   resolution = forms.IntegerField(label='Resolution (seconds)')
   ENERGY_CHOICES = (
        (1, "Normal"),
        (2, "Greedy"),
        (3, "Conserve"),
    )
   energy_use = forms.ChoiceField(label='Energy Use', choices=ENERGY_CHOICES)

class DatadelForm(forms.Form):
   device = DeviceModelChoiceField(label='Device', queryset=Device.objects.all())
   refresh_queries = forms.BooleanField(label='Refresh Queries', required=False)

class DevForm(forms.Form):
   method = forms.ChoiceField(choices=(('datagen','datagen'),('datadel','datadel')))

@csrf_exempt
def gitupdate(request):
    """
    DEPRECATED
    
    This could be reinstated by changing the Git directory seen below.
    """
    if request.method == 'POST':
        try:
            g = git.cmd.Git("/home/ubuntu/seads-git/ShR2/")
            g.pull()
            return HttpResponse(status=200)
        except: return HttpResponse(status=500)
    else: return HttpResponse(status=403)

@csrf_exempt
def echo(request):
   return HttpResponse(str(time.time()), status=200, content_type="text/plain")

@csrf_exempt
def echo_args(request, args):
   return HttpResponse(status=200)

def generate_points(start, stop, resolution, energy_use, device, channels):
   """
   Function to generate random points of data.
   
   The goal of this function was to generate data that could maybe pass
   as being semi-realistic. To do this, each circuit type has its own
   profile with an average, minumum, maximum, and cutoff wattage.
   
   These values are added/subtracted by a random number in a range
   proportional to the maximum wattage for the circuit. This gives
   a series that appears to be changing slowly over time.
   
   This function works in much the same way as the save() function
   for an :class:`microdata.models.Event`. It keeps track of the
   cumulative KWh consumed and will advance the tier level if
   the threshold is passed.
   """
   
   multiplier = 1
   if energy_use == 2: multiplier = 2
   if energy_use == 3: multiplier = .3
   wattages = {
      'Bedroom': {
         'avg':200,
         'cutoff':50,
         'max':300,
         'min':0,
         'pk': CircuitType.objects.get(name='Bedroom').pk
      },
      'Kitchen': {
         'avg':1000,
         'cutoff':500,
         'max':2000,
         'min':0,
         'pk': CircuitType.objects.get(name='Kitchen').pk
      },
      'Living Room': {
         'avg':400,
         'cutoff':50,
         'max':1000,
         'min':0,
         'pk': CircuitType.objects.get(name='Living Room').pk
      },
      
   }
   """
   wattages = {
      'Unknown':{
        'avg':800,
        'cutoff':0,
        'max':1000,
        'min':600,
        'channel': 0
      },
        'Computer':{
          'avg':200,
          'cutoff':50,
          'max':350,
          'min':0,
          'channel': 1
      },
        'Toaster':{
          'avg':20,
          'cutoff':0,
          'max':60,
          'min':0,
          'channel': 2
      },
        'Refrigerator':{
          'avg':400,
          'cutoff':100,
          'max':600,
          'min':0,
          'channel': 2
      },
        'Television':{
          'avg':100,
          'cutoff':50,
          'max':200,
          'min':0,
          'channel': 3
      },
        'Oven':{
          'avg':700,
          'cutoff':600,
          'max':1000,
          'min':0,
          'channel': 2
      },
        'Heater':{
          'avg':8000,
          'cutoff':600,
          'max':10000,
          'min':0,
          'channel': 1
      },
    }
    """
   for appliance in wattages:
      for value in appliance:
         value *= multiplier
   db = influxdb.InfluxDBClient(settings.INFLUXDB_URI, 8086, "root", "root", "seads")
   count = 0

   kilowatt_hours_monthly = device.kilowatt_hours_monthly
   kilowatt_hours_daily = device.kilowatt_hours_daily
   max_percentage_of_baseline = device.devicewebsettings.current_tier.max_percentage_of_baseline
   current_tier = device.devicewebsettings.current_tier

   this_year = datetime.now().year
   summer_start = datetime(year=this_year,month=device.devicewebsettings.territories.all()[0].summer_start,day=1)
   winter_start = datetime(year=this_year,month=device.devicewebsettings.territories.all()[0].winter_start,day=1)
   summer_rate = device.devicewebsettings.territories.all()[0].summer_rate
   winter_rate = device.devicewebsettings.territories.all()[0].winter_rate
   current_season = 'summer'
   if (summer_start <= datetime.now() < winter_start) == False:
      current_season = 'winter'
      
   TierSeriesHelper(serial=str(device.serial), time=start, level=int(device.devicewebsettings.current_tier.tier_level))
   
   for i in numpy.arange(start, stop, resolution):
      kwh = 0.0
      point_list = [i]
      for channel in channels:
         wattage = wattages[channel.name]['avg'] + random.uniform(-wattages[channel.name]['max']*0.1,wattages[channel.name]['max']*0.1)
         wattage_to_append = 0
         if wattage > wattages[channel.name]['max']:
            wattage_to_append = wattages[channel.name]['max']
         elif wattage < wattages[channel.name]['cutoff']:
            wattage_to_append = 0
         elif wattage < wattages[channel.name]['min']:
            wattage_to_append = wattages[channel.name]['min']
         else:
            wattages[channel.name]['avg'] = wattage
            wattage_to_append = wattage
         kwh = (wattage_to_append/1000.0)*(resolution)*(1/3600.0)
         kilowatt_hours_monthly += kwh
         kilowatt_hours_daily += kwh
         if (max_percentage_of_baseline != None):
          max_kwh_for_tier = (max_percentage_of_baseline/100.0)*summer_rate*31.0
          if current_season == 'winter':
             max_kwh_for_tier = (current_tier.max_percentage_of_baseline/100.0)*winter_rate*31.0
          if (kilowatt_hours_monthly > max_kwh_for_tier):
             current_tier = device.devicewebsettings.current_tier
             device.devicewebsettings.current_tier = device.devicewebsettings.rate_plans.all()[0].get(tier_level = current_tier.tier_level +1)
             device.devicewebsettings.save()
             device.save()
             tier_dict['points'] = [[i,device.devicewebsettings.current_tier.tier_level]]
             db.write_points([tier_dict])
         cost = current_tier.rate * kwh
         circuit_pk = wattages[channel.name]['pk']
         EventSeriesHelper(serial=str(device.serial), time=i, wattage=wattage_to_append, circuit_pk=str(circuit_pk), cost=cost)
   EventSeriesHelper.commit()
   queries = db.query('show continuous queries')[0]['points']
   # drop old queries
   serial = str(device.serial)
   for q in queries:
     if 'device.'+serial in q[2]:
         db.query('drop continuous query '+str(q[1]))
   # add new queries
   db.query('select * from device.'+serial+' into device.'+serial+'.[circuit_pk]')
   db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1y) into 1y.:series_name')
   db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1M) into 1M.:series_name')
   db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1w) into 1w.:series_name')
   db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1d) into 1d.:series_name')
   db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1h) into 1h.:series_name')
   db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1m) into 1m.:series_name')
   db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1s) into 1s.:series_name')
   db.query('select sum(cost) from "device.'+serial+'" into cost.device.'+serial)
   success = "Added {0} points successfully".format(count)
   device.kilowatt_hours_monthly = kilowatt_hours_monthly
   device.kilowatt_hours_daily = kilowatt_hours_daily
   device.save()
   return success

@csrf_exempt
def influxgen(request):
   success = ""
   if request.method == 'POST':
      form = DatagenForm(request.POST)
      if form.is_valid():
         device = form.cleaned_data['device']
         channels = form.cleaned_data['channels']
         start = form.cleaned_data['start']
         stop = form.cleaned_data['stop']
         resolution = form.cleaned_data['resolution']
         energy_use = form.cleaned_data['energy_use']
         success = generate_points(start, stop, resolution, energy_use, device, channels)

   else:
      form = DatagenForm()
   title = "Debug - Data Generation (InfluxDB)"
   description = "Use this form to submit random generated data for the device chosen."
   return render(request, 'debug.html', {'title':title,'description':description,'form':form, 'success':success})

def datagen(request):
   """
   DEPRECATED
   
   See `influxgen <debug.html#debug.views.influxgen>`_.
   """
   success = ""
   if request.method == 'POST':
      form = DatagenForm(request.POST)
      if form.is_valid():
         device = form.cleaned_data['device']
         appliances = form.cleaned_data['appliances']
         start = form.cleaned_data['start']
         stop = form.cleaned_data['stop']
         resolution = form.cleaned_data['resolution']
         wattages = {'Unknown':{'avg':700, 'stdev':20}, 'Computer':{'avg':100, 'stdev':50}, 'Toaster':{'avg':20, 'stdev':20}, 'Refrigerator':{'avg':400,'stdev':200}, 'Television':{'avg':60,'stdev':60}}
         count = 0
         for i in numpy.arange(start, stop, resolution):
             for appliance in appliances:
                 wattage = wattages[appliance.name]['avg'] + random.uniform(-wattages[appliance.name]['stdev'],wattages[appliance.name]['stdev'])
                 event = Event(device=device, timestamp=i*1000, wattage=wattage, appliance=appliance)
                 event.save()
                 count += 1
         success = "Added {0} events successfully".format(count)
   else:
      form = DatagenForm()
   title = "Debug - Data Generation"
   description = "Use this form to submit random generated data for the device chosen."
   return render(request, 'debug.html', {'title':title,'description':description,'form':form, 'success':success})

@csrf_exempt
def influxdel(request):
   success = ""
   if request.method == 'POST':
      form = DatadelForm(request.POST)
      count = 0
      if form.is_valid():
         device = form.cleaned_data['device']
         serial = str(device.serial)
         refresh_queries = form.cleaned_data['refresh_queries']
         db = influxdb.InfluxDBClient(settings.INFLUXDB_URI, 8086, "root", "root", "seads")
         if refresh_queries is False:
            device.kilowatt_hours_monthly = 0
            device.kilowatt_hours_daily = 0
            device.save()
            rate_plan = device.devicewebsettings.rate_plans.all()[0]
            tiers = Tier.objects.filter(rate_plan=rate_plan)
            for tier in tiers:
              if tier.tier_level == 1:
                device.devicewebsettings.current_tier = tier
            device.devicewebsettings.save()
            tier_dict = {}
            tier_dict['name'] = "tier.device."+str(device.serial)
            tier_dict['columns'] = ['tier_level']
            tier_dict['points'] = [[1]]
            db.write_points([tier_dict])
            series = db.query('show series')[0]['points']
            rg = re.compile('device.'+serial)
            for s in series:
               if rg.search(s[1]):
                    db.query('drop series "'+s[1]+'"')
            events = Event.objects.filter(device=device)
            events.delete()
            queries = db.query('show continuous queries')[0]['points']
            # drop old queries
            for q in queries:
               if 'device.'+serial in q[2]:
                  db.query('drop continuous query '+str(q[1]))
         else:
            queries = db.query('show continuous queries')[0]['points']
            # drop old queries
            for q in queries:
              if 'device.'+serial in q[2]:
                  db.query('drop continuous query '+str(q[1]))
               
            # add new queries
            db.query('select * from device.'+serial+' into device.'+serial+'.[circuit_pk]')
            db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1y) into 1y.:series_name')
            db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1M) into 1M.:series_name')
            db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1w) into 1w.:series_name')
            db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1d) into 1d.:series_name')
            db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1h) into 1h.:series_name')
            db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1m) into 1m.:series_name')
            db.query('select mean(wattage) from /^device.'+serial+'.*/ group by time(1s) into 1s.:series_name')
            db.query('select sum(cost) from "device.'+serial+'" into cost.device.'+serial)
           
   else:
      form = DatadelForm()
   title = "Debug - Data Deletion"
   description = "Use this form to delete data for the device chosen."
   return render(request, 'debug.html', {'title':title,'description':description,'form':form, 'success':success})
   

def datadel(request):
   """
   DEPRECATED
   
   See `influxdel <debug.html#debug.views.influxdel>`_.
   """
   success = ""
   if request.method == 'POST':
      form = DatadelForm(request.POST)
      count = 0
      if form.is_valid():
         device = form.cleaned_data['device']
         events = Event.objects.filter(device=device)
         count = len(events)
         events.delete()
         success = "Deleted {0} events successfully".format(count)
   else:
      form = DatadelForm()
   title = "Debug - Data Deletion"
   description = "Use this form to delete data for the device chosen."
   return render(request, 'debug.html', {'title':title,'description':description,'form':form, 'success':success})
