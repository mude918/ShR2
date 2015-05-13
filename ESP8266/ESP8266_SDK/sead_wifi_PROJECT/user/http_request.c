/*
 * Henry Crute
 * hcrute@ucsc.edu
 * 	
 * Implementation of tcp connection, and sending data with a request
 * 
 */

#include "c_types.h"
#include "espmissingincludes.h"
#include "user_interface.h"
#include "espconn.h"
#include "mem.h"
#include "ssl/cert.h"
#include "ssl/private_key.h"

#include "uart.h"
#include "send_recv_port.h"
#include "http_request.h"
#include "extralib.h"

//server name and device ID number
#define SERVER_NAME "seads.brabsmit.com"
#define DEVICE_ID	"3"

//test string to send to server
#define GET_ECHO 	"GET /echo/ HTTP/1.1\r\n"\
					"User-Agent: ESP8266\r\n"\
					"Host: seads.brabsmit.com\r\n"\
					"Accept: */*\r\n\r\n"

//"Authorization: Token 0d1e0f4b56e4772fdb440abf66da8e2c1df799c0\r\n"

#define GET_SETTINGS 	"GET /api/settings-api/%s/ HTTP/1.1\r\n"\
						"User-Agent: ESP8266\r\n"\
						"Host: seads.brabsmit.com\r\n"\
						"Accept: */*\r\n\r\n"

// http request to create the device
#define POST_DEVICE	"POST /api/device-api/ HTTP/1.1\r\n"\
					"User-Agent: ESP8266\r\n"\
					"Host: seads.brabsmit.com\r\n"\
					"Accept: */*\r\n"\
					"Content-Type: application/x-www-form-urlencoded\r\n"\
					"Content-Length: %u\r\n\r\n"\
					"{\"serial\":\"%s\"}"

//the preamble of the post request
#define POST_REQUEST "POST /api/event-api/ HTTP/1.1\r\n"\
					 "User-Agent: ESP8266\r\n"\
					 "Host: seads.brabsmit.com\r\n"\
					 "Accept: */*\r\n"\
					 "Content-Type: application/x-www-form-urlencoded\r\n"\
					 "Content-Length: %u\r\n\r\n%s"

//format string for json data
#define JSON_DATA	"{\"device\":\"/api/device-api/%s/\","\
					"\"time\":[\"%s\", \"%x\"],"\
					"\"dataPoints\":[%s]}"

//one data point
#define DATA_POINT	"{\"wattage\":%u}"
//comma separation
#define COMMA		","

//approximately in howevery many times send http request is called
#define WIFI_CONFIG_TIMEOUT 10

bool send_get_config = false;
//connection tries
uint8_t connect_try = 0;

//time offset
uint64_t offset = 0;

uint16_t frequency = 1;
//len 81
//AT+CIPSEND=81

//prototypes
static void networkSentCb(void *);
static void networkRecvCb(void *, char *, unsigned short);
static void networkConnectedCb(void *);
static void networkReconCb(void *, sint8);
static void networkDisconCb(void *);
void network_start(void);
sint8 package_send(espconn *);
sint8 config_send(espconn *);

//initializing connection variables!
static espconn serv_conn;
static ip_addr_t serv_ip;

//pointer to the data we want to send:
//TODO: localize this pointer in function calls!
circular_send_buffer_t *send_buffer_ptr = NULL;

/**
  * @brief  Prints debug information to uart about the espconn state
  * @param  ESP Connection struct
  * @retval None
  */
void print_espconn_state(espconn *serv_connection) {
	os_printf("espconn state is: %d\r\n", serv_connection->state);
}

/**
  * @brief  Formatts and packages data to be sent, and sends it
  * @param  The server connection
  * @retval True if succeeded, false if failed
  */
sint8 ICACHE_FLASH_ATTR
package_send(espconn *serv_conn) {
	//init variables
	char timebuffer[21] = "";
	uint16_t chars_written = 0;
	uint16_t i = 0;
	char *data_points = (char *)os_malloc(send_buffer_ptr->count * 20);
	//send regular data
	os_printf("\r\nData Count: %d\r\n", send_buffer_ptr->count);
	char *data_ptr = data_points;
	inttohexstring(send_buffer_ptr->buffer[send_buffer_ptr->tail].
		timestamp + offset, timebuffer);
	//loop over all available data to send
	chars_written = os_sprintf(data_ptr, DATA_POINT,
		send_buffer_ptr->buffer[send_buffer_ptr->tail].wattage);
	pop_pop_buffer();
	//ensured that there is at least one data point
	for (i = 0; send_buffer_ptr->count > 0; i++) {
		data_ptr += chars_written;
		chars_written = os_sprintf(data_ptr, COMMA);
		data_ptr += chars_written;
		chars_written = os_sprintf(data_ptr, DATA_POINT,
			send_buffer_ptr->buffer[send_buffer_ptr->tail].wattage);
		pop_pop_buffer();
	}
	char *json_data = (char *)os_malloc(100 + strlen(data_points));
	//concatonates json data
	chars_written = os_sprintf(json_data, JSON_DATA, DEVICE_ID,
		timebuffer, frequency, data_points);
	os_free(data_points);
	//allocates final data buffer
	char *send_data = (char *)os_malloc(200 + strlen(json_data));
	//concatonates the data to send with the http header
	chars_written = os_sprintf(send_data, POST_REQUEST, chars_written,
		json_data);
	os_free(json_data);
	os_printf("Final Buf Len: %d\r\nActual Data Len: %d\r\n",
		300 + i * 16, strlen(send_data));
	//os_printf("%s\r\n", send_data);
	//send the data.
	sint8 retvalue = espconn_sent(serv_conn, (uint8 *)send_data,
		strlen(send_data));
	//free the data
	os_free(send_data);
	return retvalue;
}

/**
  * @brief  Server connection callback on data sent
  * @param  The esp connection
  * @retval None
  */
static void ICACHE_FLASH_ATTR
networkSentCb(void *arg) {
	os_printf("sent\r\n");
	print_espconn_state((espconn *)arg);
	//incriments circular buffer tail (POP POP)
	//old part where we would pop pop for single data points
}

/**
  * @brief  Received callback for server connection
  * @param  The esp connection
  * @retval None
  */
static void ICACHE_FLASH_ATTR
networkRecvCb(void *arg, char *data, unsigned short len) {
	uint16_t i;
	os_printf("recv\r\n");
	//print out what was received
	espconn *serv_conn=(espconn *)arg;
	//if we received a 404, then create the device
	if (strncmp(data, "HTTP/1.1 404", 12) == 0) {
		char send_data[256] = "";
		os_printf("Creating device\r\n");
		os_sprintf(send_data, POST_DEVICE,
			13 + strlen(DEVICE_ID), DEVICE_ID);
		//possibly need to move somewhere else
      
		espconn_sent(serv_conn,(uint8 *)send_data,strlen(send_data));
	//received a 200, then we successfully got settings
	} else if(strncmp(data, "HTTP/1.1 200", 12) == 0) {
		char *temp_ptr;
		for (temp_ptr = data;
			strncmp(temp_ptr, "seconds", 7) != 0;
			temp_ptr++) {
			if (temp_ptr == NULL) {
				return;
			}	
		}
		temp_ptr += 9;
		uint16_t rate = stringtoint(temp_ptr);
		for (; strncmp(temp_ptr, "date_now", 8) != 0;
			temp_ptr++) {
			if (temp_ptr == NULL) {
				return;
			}
		}
		temp_ptr += 10;
		offset = stringtoint(temp_ptr);
		set_rate(rate);
		os_printf("rate is %u\r\noffset is %llu\r\n", rate,
			(long long unsigned int)offset);
		done_config();
	//received a 201, then we successfully posted data
	} else if(strncmp(data, "HTTP/1.1 201", 12) == 0) {
		os_printf("CREATED\r\n");
	} else {
		for (i = 0; i < 12; i++) {
			uart0_putChar(data[i]);
		}
		uart0_putChar('\r');
		uart0_putChar('\n');
	}
	print_espconn_state(serv_conn);
	return;
}

/**
  * @brief  Server connection callback on network initially connected
  * 		Sends a http get request for config when connected
  * @param  The esp connection
  * @retval None
  */
static void ICACHE_FLASH_ATTR
networkConnectedCb(void *arg) {
	os_printf("conn\r\n");
	//config_send((espconn *)arg);
	//os_printf("conn_end\r\n");
	print_espconn_state((espconn *)arg);
}

/**
  * @brief  Server connection callback on disconnect with reconnect
  * @param  The esp connection
  * @retval None
  */
static void ICACHE_FLASH_ATTR
networkReconCb(void *arg, sint8 err) {
	os_printf("rcon\r\n");
	espconn_disconnect((espconn *)arg);
	print_espconn_state((espconn *)arg);
}

/**
  * @brief  Server connection callback on disconnect
  * @param  The esp connection
  * @retval None
  */
static void ICACHE_FLASH_ATTR
networkDisconCb(void *arg) {
	os_printf("dcon\r\n");
	espconn_disconnect((espconn *)arg);
	print_espconn_state((espconn *)arg);
}

/**
  * @brief  Server connection callback on server connection found
  * @param  The esp connection, server ip, and hostname
  * @retval None
  */
static void ICACHE_FLASH_ATTR
networkServerFoundCb(const char *name, ip_addr_t *serv_ip, void *arg) {
	//initializing connection arguments
	static esp_tcp tcp;
	espconn *serv_conn=(espconn *)arg;
	if (serv_ip==NULL) {
		os_printf("\r\nNS lookup failed\r\n");
		return;
	}
	//the destination IP address from NS lookup
	os_printf("\r\nDST: %d.%d.%d.%d\r\n",
	*((uint8 *)&serv_ip->addr), *((uint8 *)&serv_ip->addr + 1),
	*((uint8 *)&serv_ip->addr + 2), *((uint8 *)&serv_ip->addr + 3));
	//specify the connection to be tcp
	serv_conn->type=ESPCONN_TCP;
	serv_conn->state=ESPCONN_NONE;
	serv_conn->proto.tcp=&tcp;
	//specify port number 80 and ip address
	serv_conn->proto.tcp->local_port=espconn_port();
	serv_conn->proto.tcp->remote_port=80;
	os_memcpy(serv_conn->proto.tcp->remote_ip, &serv_ip->addr, 4);
	//specify callback functions for different situations
	espconn_regist_connectcb(serv_conn, networkConnectedCb);
	espconn_regist_disconcb(serv_conn, networkDisconCb);
	espconn_regist_reconcb(serv_conn, networkReconCb);
	espconn_regist_recvcb(serv_conn, networkRecvCb);
	espconn_regist_sentcb(serv_conn, networkSentCb);
	//debug
	print_espconn_state(serv_conn);
	espconn_connect(serv_conn);
	//debug
	print_espconn_state(serv_conn);
}

/**
  * @brief  Server connection initialization/reinitialization
  * @param  None
  * @retval None
  */
void ICACHE_FLASH_ATTR
network_start(void) {
	os_printf("Looking up server\r\n");
	espconn_gethostbyname(&serv_conn, SERVER_NAME, &serv_ip,
						  networkServerFoundCb);
	print_espconn_state(&serv_conn);
}

/**
  * @brief  calls send_http_request in a way to send config get request
  * @param  none
  * @retval True if succeeded, false if failed
  */
bool ICACHE_FLASH_ATTR
get_http_config(void) {
	return send_http_request(NULL);
}

/**
  * @brief  Formatts and sends a configuration request to the server
  * @param  The server connection
  * @retval True if succeeded, false if failed
  */
sint8 ICACHE_FLASH_ATTR
config_send(espconn *serv_conn) {
	os_printf("Sending get config\r\n");
	char send_data[256] = "";
	//format config request
	os_sprintf(send_data, GET_SETTINGS, DEVICE_ID);
	//send config GET request
	return espconn_sent(serv_conn, (uint8 *)send_data,
			strlen(send_data));
}

/**
  * @brief  Main logic controlling the http connection, and requests
  * @param  Data to send
  * @retval True if succeeded, false if failed to send
  */
bool ICACHE_FLASH_ATTR
send_http_request(circular_send_buffer_t *temp) {
	bool return_value = false;
	//check to see if we have an IP address and we are in STA mode
	//1 stands for STA mode 2 is AP and 3 is both STA+AP
	if (wifi_station_get_connect_status() == STATION_GOT_IP &&
		wifi_get_opmode() == 1) {
		//reset the connect try variable for wifi config
		connect_try = 0;
		send_buffer_ptr = temp;
		//if the server connection isn't up???? start it
		if (serv_conn.state == ESPCONN_NONE) {
			network_start();
		} else if (serv_conn.state == ESPCONN_CONNECT &&
				   send_buffer_ptr == NULL) {
			//send config function
			sint8 d = config_send(&serv_conn);
		} else if (serv_conn.state == ESPCONN_CONNECT) {
			//send data function
			sint8 d = package_send(&serv_conn);
		} else if (serv_conn.state == ESPCONN_CLOSE) {
			//not sure if this is right?
			//espconn_disconnect(&serv_conn);
			espconn_delete(&serv_conn);
			network_start();
		}
		return_value = true;
	} else if (wifi_get_opmode() == 1) {
		//go back into AP mode after any number unsuccessfull attempts
		//to connect to the access point
		if (connect_try++ >= WIFI_CONFIG_TIMEOUT) {
			connect_try = 0;
			os_printf("Resetting for wifi config\r\n");
			wifi_set_opmode(2);
			system_restart();
		} else {
			os_printf("Going into AP mode after %d more attempts\r\n",
					  WIFI_CONFIG_TIMEOUT - connect_try);
		}
		return_value = false;
	} else {
		os_printf("No ip addr and not in STA mode\r\n");
		return_value = false;
	}
	//finished with initiating networking requests
	done_sending();
	return return_value;
}
