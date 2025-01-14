import time
import typing
from datetime import datetime
from typing import Optional

import httpx
import pytz
from ics import Calendar, Event
from langchain_core.tools import tool
from nc_py_api import Nextcloud
from nc_py_api.ex_app import LogLvl
from pydantic import BaseModel, ValidationError

from logger import log


def get_tools(nc: Nextcloud):

	@tool
	def list_calendars():
		"""
		List all existing calendars by name
		:return:
		"""
		principal = nc.cal.principal()
		calendars = principal.calendars()
		return ", ".join([cal.name for cal in calendars])

	@tool
	def schedule_event(calendar_name: str, title: str, description: str, start_date: str, end_date: str, start_time: Optional[str], end_time: Optional[str], location: Optional[str], timezone: Optional[str]):
		"""
		Crete a new event in a calendar. Omit start_time and end_time parameters to create an all-day event.
		:param calendar_name: The name of the calendar to add the event to
		:param title: The title of the event
		:param description: The description of the event
		:param start_date: the start date of the event in the following form: YYYY-MM-DD e.g. '2024-12-01'
		:param end_date: the end date of the event in the following form: YYYY-MM-DD e.g. '2024-12-01'
		:param start_time: the start time in the following form: HH:MM AM/PM e.g. '3:00 PM'
		:param end_time: the start time in the following form: HH:MM AM/PM e.g. '4:00 PM'
		:param location: The location of the event
		:param timezone: Timezone (e.g., 'America/New_York').
		:return: bool
		"""

		# Parse date and times
		start_date = datetime.strptime(start_date, "%Y-%m-%d")
		end_date = datetime.strptime(end_date, "%Y-%m-%d")

		# Combine date and time
		if start_time and end_time:
			start_time = datetime.strptime(start_time, "%I:%M %p").time()
			end_time = datetime.strptime(end_time, "%I:%M %p").time()

			start_datetime = datetime.combine(start_date, start_time)
			end_datetime = datetime.combine(end_date, end_time)
		else:
			start_datetime = start_date
			end_datetime = end_date

		# Set timezone
		tz = pytz.timezone(timezone)
		start_datetime = tz.localize(start_datetime)
		end_datetime = tz.localize(end_datetime)


		# Create event
		c = Calendar()
		e = Event()
		e.name = title
		e.begin = start_datetime
		e.end = end_datetime
		e.description = description
		e.location = location

		# Add event to calendar
		c.events.add(e)

		principal = nc.cal.principal()
		calendars = principal.calendars()
		calendar = {cal.name: cal for cal in calendars}[calendar_name]
		calendar.add_event(str(c))

		return True


	## Talk

	@tool
	def list_talk_conversations():
		"""
		List all conversations in talk
		:return:
		"""
		conversations = nc.talk.get_user_conversations()

		return ", ".join([conv.display_name for conv in conversations])


	@tool
	def send_message_to_conversation(conversation_name: str, message: str):
		"""
		List all conversations in talk
		:param message: The message to send
		:param conversation_name: The name of the conversation to send a message to
		:return:
		"""
		conversations = nc.talk.get_user_conversations()
		conversation = {conv.display_name: conv for conv in conversations}[conversation_name]
		nc.talk.send_message(message, conversation)

		return True

	@tool
	def list_messages_in_conversation(conversation_name: str, n_messages: int = 30):
		"""
		List messages of a conversation in talk
		:param conversation_name: The name of the conversation to list messages of
		:param n_messages: The number of messages to receive
		:return:
		"""
		conversations = nc.talk.get_user_conversations()
		conversation = {conv.display_name: conv for conv in conversations}[conversation_name]
		return [f"{m.timestamp} {m.actor_display_name}: {m.message}" for m in nc.talk.receive_messages(conversation, False, n_messages)]

	@tool
	def get_coordinates_for_address(address: str) -> (str, str):
		"""
		Calculates the coordinates for a given address
		:param address: the address to calculate the coordinates for
		:return: a tuple of latitude and longitude
		"""
		res = httpx.get('https://nominatim.openstreetmap.org/search', params={'q': address, 'format': 'json', 'addressdetails': '1', 'extratags': '1', 'namedetails': '1', 'limit': '1'})
		json = res.json()
		if 'error' in json:
			raise Exception(json['error'])
		if len(json) == 0:
			raise Exception(f'No results for address {address}')
		return json[0]['lat'], json[0]['lon']


	@tool
	def get_current_weather_for_coordinates(lat: str, lon: str) -> dict[str, typing.Any]:
		"""
		Retrieve the current weather for a given latitude and longitude
		:param lat: Latitude
		:param lon: Longitude
		:return:
		"""
		res = httpx.get('https://api.met.no/weatherapi/locationforecast/2.0/compact', params={
			'lat': lat,
			'lon': lon,
		})
		json = res.json()
		if not 'properties' in json or not 'timeseries' in json['properties'] or not json['properties']['timeseries']:
			raise Exception('Could not retrieve weather for coordinates')
		return json['properties']['timeseries'][0]['data']['instant']['details']


	class Task(BaseModel):
		id: int
		status: str
		output: dict[str, typing.Any] | None = None

	class Response(BaseModel):
		task: Task

	@tool
	def ask_context_chat(question: str):
		"""
		Ask the context chat oracle, which knows all of the user's documents, a question about them
		:param question: The question to ask
		:return: the answer from context chat
		"""

		task_input = {
			'prompt': question,
			'scopeType': 'none',
			'scopeList': [],
			'scopeListMeta': '',
		}
		response = nc.ocs(
			"POST",
			"/ocs/v1.php/taskprocessing/schedule",
			json={"type": "context_chat:context_chat", "appId": "context_agent", "input": task_input},
		)

		try:
			task = Response.model_validate(response).task
			log(nc, LogLvl.DEBUG, task)

			i = 0
			# wait for 30 minutes
			while task.status != "STATUS_SUCCESSFUL" and task.status != "STATUS_FAILED" and i < 60 * 6:
				time.sleep(5)
				i += 1
				response = nc.ocs("GET", f"/ocs/v1.php/taskprocessing/task/{task.id}")
				task = Response.model_validate(response).task
				log(nc, LogLvl.DEBUG, task)
		except ValidationError as e:
			raise Exception("Failed to parse Nextcloud TaskProcessing task result") from e

		if task.status != "STATUS_SUCCESSFUL":
			raise Exception("Nextcloud TaskProcessing Task failed")

		if not isinstance(task.output, dict) or "output" not in task.output:
			raise Exception('"output" key not found in Nextcloud TaskProcessing task result')

		return task.output['output']

	dangerous_tools = [
		schedule_event,
		send_message_to_conversation
	]
	safe_tools = [
		list_calendars,
		list_talk_conversations,
		list_messages_in_conversation,
		ask_context_chat,
		get_coordinates_for_address,
		get_current_weather_for_coordinates,
	]

	return safe_tools, dangerous_tools
