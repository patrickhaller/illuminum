import time,json,os,base64,hashlib,string,random
from clients import alert_event,webcam_viewer,det_state
import server_m2m
import server_ws
import send_mail
import p
from rule_manager import *
from sql import *

#***************************************************************************************#
#***************************************** m2m *****************************************#
#***************************************************************************************#
# M2M functions are used to handle all messages between the server machine and the
# camera machine, M<->M. There should be no user interaction involved, as every message
# shall go over this server.
# There is a thread called server_m2m running in the background and waiting for incoming
# packets (low level). Whenever new camera is coming online the function recv_m2m_con_q_handle
# shall be triggered. This function will queue the data to recv_m2m_con_q and finish.
#
# The main loop shall run the function recv_m2m_con_dq_handle and check if our queue has
# elements to be processed. if this is the case the function will call recv_m2m_con_handle
# to process the connection change.
#
# The same principal is used to receive messages from the clients
#
# If recv_m2m_con_handle or recv_m2m_msg_handle or anyone else has to send something back
# to the cam, then they should append a message to the msq_q_m2m structure
# This structure will be checked by the main loop as well and if it contains some they
# will be send to the correct m2m device mentioned in the CLI argument. This way all messages
# and state changes are aligned in FIFO structures
#******************************************************#

################## M2M CONNECTION #########################
#******************************************************#
# recv_m2m_con_q_handle will be called by the server structure and shall just append
# the message to the queue
def recv_m2m_con_q_handle(data,m2m):
	recv_m2m_con_q.append((data,m2m))
#******************************************************#

#******************************************************#
# recv_m2m_con_dq_handle will be called in the main loop and will forward a message
# to the recv_m2m_con_handle if there is one in the queue
def recv_m2m_con_dq_handle():
	ret=0
	if(len(recv_m2m_con_q)>0):
		ret=1
		recv_con=recv_m2m_con_q[0]
		recv_m2m_con_q.remove(recv_con)
		recv_m2m_con_handle(recv_con[0],recv_con[1])
	return ret
#******************************************************#

#******************************************************#
# recv_m2m_con_handle will be called by the dequeue handle above.
# it shall handle disconnect situations to avoid that we are talking to dead sockets
def recv_m2m_con_handle(data,m2m):
	# this function is is used to be callen if a m2m disconnects, we have to update all ws clients
	#print("[A_m2m "+time.strftime("%H:%M:%S")+"] connection change")
	if(data=="disconnect"):
		p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+str(m2m.mid)+"' disconneted","l")
		db.update_last_seen_m2m(m2m.mid,"") #<- this returns bad file descriptor

		# try to find that m2m in all ws clients lists, so go through all clients and their lists
		for ws in server_ws.clients:
			for viewer in ws.v2m:
				if(viewer==m2m):
					p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] releasing '"+ws.login+"' from "+m2m.mid,"l")
					ws.v2m.remove(viewer)
					msg={}
					msg["cmd"]="disconnect"
					msg["mid"]=m2m.mid
					msg["area"]=m2m.area
					msg["account"]=m2m.account
					msg["detection"]=m2m.detection
					msg_q_ws.append((msg,ws))
		try:
			server_m2m.clients.remove(m2m)
		except:
			ignore=1
#******************************************************#
################## M2M CONNECTION #########################

################## M2M MESSAGE #########################
#******************************************************#
# recv_m2m_msg_q_handle will be called by the server structure shall just append
# the message to the queue
def recv_m2m_msg_q_handle(data,m2m):
	recv_m2m_msg_q.append((data,m2m))
#******************************************************#

#******************************************************#
# recv_m2m_msg_dq_handle will be called in the main loop and will forward a message
# to the recv_m2m_handle if there is one in the queue
def recv_m2m_msg_dq_handle():
	ret=0
	if(len(recv_m2m_msg_q)>0):
		ret=1
		recv_msg=recv_m2m_msg_q[0]
		recv_m2m_msg_q.remove(recv_msg)
		recv_m2m_msg_handle(recv_msg[0],recv_msg[1])
	return ret
#******************************************************#

#******************************************************#
# this function, called by the dequeue above will handles all
# incoming messages and generate responses, which will be stored in the msg_q_m2m
def recv_m2m_msg_handle(data,m2m):
	global msg_q_m2m, msg_q_ws, rm, db
	# decode msg from string to dicc
	try:
		enc=json.loads(data)
	except:
		enc=""
		p.rint("-d--> json decoding failed on:" + data,"d")

	#print("m2m:"+str(m2m.port)+"/"+str(m2m.ip))
	if(type(enc) is dict):
		# if the message would like to be debugged
		if(enc.get("debug",0)==1):
			for key, value in enc.items() :
				p.rint("-d-->Key:'"+key+"' / Value:'"+str(value)+"'","d")

		# set last_comm token
		m2m.last_comm=time.time()

		#********* msg handling **************#
		# assuming that we could decode the message from json to dicc: we have to distingush between the commands:
		if(m2m.logged_in==0 and enc.get("cmd")!="login" and enc.get("cmd")!="prelogin"):
			p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] A client tried to interact without beeing logged in","l")
			# send bad ack
			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["ok"]=-2 # not logged in
			msg_q_m2m.append((msg,m2m))

		#### pre login challange for M2M
		elif(enc.get("cmd")=="prelogin"):
			m2m.challange=get_challange()
			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["challange"]=m2m.challange
			msg_q_m2m.append((msg,m2m))
			#print("received prelogin request, sending challange "+m2m.challange)


		#### login try to set the logged_in to 1 to upload files etc, for M2M
		elif(enc.get("cmd")=="login"):
			msg={}
			msg["cmd"]=enc.get("cmd")

			# data base has to give us this values based on enc.get("mid")
			db_r=db.get_data(enc.get("mid"))
			#print("Ergebniss der datenbank:")
			#print(db_r)

			if(type(db_r) is int): #user not found
				#print("db error")
				p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+str(enc.get("mid"))+"' not found in DB, log-in: failed","l")
				msg["ok"]=-3 # not logged in
			else:
				h = hashlib.new('ripemd160')
				h.update(str(db_r["pw"]+m2m.challange).encode("UTF-8"))
				#print("total to code="+(str(db["pw"]+m2m.challange)))
				#print("result="+h.hexdigest()+" received: "+enc.get("client_pw"))

				# check parameter
				if(h.hexdigest()==enc.get("client_pw")):
					m2m.logged_in=1
					m2m.mid=enc.get("mid")
					m2m.state=enc.get("state")
					m2m.alert=alert_event() 	# TODO we should fill the alert with custom values like max photos etc
					msg["ok"]=1 # logged in

					# get area and account based on database value for this mid
					m2m.account=db_r["account"]
					m2m.area=db_r["area"]
					m2m.alias=db_r["alias"]
					m2m.longitude=db_r["longitude"]
					m2m.latitude=db_r["latitude"]
					m2m.brightness_pos=db_r["brightness_pos"]
					m2m.color_pos=db_r["color_pos"]
					
					msg["alias"]=m2m.alias		# this goes to the m2m
					msg["mRed"]=db_r["mRed"]
					msg["mGreen"]=db_r["mGreen"]
					msg["mBlue"]=db_r["mBlue"]
					

					# add rules to the rule manager for this area if it wasn there before
					# first check if the account is known to the rule manager at all and add it if not
					#print("### rm debug ###")
					#rm.print_all()
					#print("### rm debug ###")
					if(not(rm.is_account(m2m.account))):
						#print("account did not exist, adding")
						new_rule_account=rule_account(m2m.account)
						rm.add_account(new_rule_account)

					# then check the same for the area, if there was NO m2m and NO ws connected, the area wont be in the rm, otherwise it should
					if(not(rm.is_area_in_account(m2m.account,m2m.area))):
						#print("area did not exist, adding")
						new_area=area(m2m.area,m2m.account,db) # will load rule set on its own from the database
						rm.add_area_to_account(m2m.account,new_area)

						# if the area wasn in the rule manager we have to
						# check the state, as there could be a time based trigger that wasn executed
						# lets do it for all areas for this account (kind of a waste but its is very quick)
						#print("### rm debug ###")
						#rm.print_all()
						#print("### rm debug ###")
						#print("checking for this account")
						acc=rm.get_account(m2m.account)
						if(acc!=-1):
							for b in acc.areas:
								detection_state=b.check_rules(1) 	# get the state, check and use db
								db.update_det("m2m",m2m.account,m2m.area,detection_state)
								#print("area "+str(b.area)+" should be")
								#print(detection_state)

					# get detecion state based on db
					db_r2=db.get_state(m2m.area,m2m.account)
					#FIND ME
					m2m.detection=int(db_r2["state"])
					msg["detection"]=m2m.detection

					# search for all (active and logged-in) viewers for this client (same account)
					info_viewer=0
					#print("my m2m account is "+m2m.account)
					for viewer in server_ws.clients:
						#print("this client has account "+viewer.account)
						if(viewer.account==m2m.account):
							# introduce them to each other
							connect_ws_m2m(m2m,viewer,1)
							info_viewer+=1
							# we could send a message to the box to tell the if there is a visitor logged in ... but they don't care
					p.m2m_login(m2m,info_viewer)
						
					try:
						ip=m2m.conn.getpeername()[0]
					except:
						ip="???"
					db.update_last_seen_m2m(m2m.mid,ip)
						
				else:
					p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+str(m2m.mid)+"' log-in: failed","l")
					msg["ok"]=-2 # not logged in
			# send message in any case
			msg_q_m2m.append((msg,m2m))

		#### heartbeat, for M2M
		elif(enc.get("cmd")=="m2m_hb"):
			# respond
			p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+m2m.mid+"' / '"+m2m.alias+"' HB updating "+str(len(m2m.m2v))+" clients","h")
			msg={}
			msg["mid"]=m2m.mid
			msg["cmd"]=enc.get("cmd")
			msg["ok"]=1
			msg_q_m2m.append((msg,m2m))

			# tell subscribers
			msg={}
			msg["mid"]=m2m.mid
			msg["cmd"]=enc.get("cmd")
			msg["ts"]=time.time()
			for subscriber in m2m.m2v:
				#print("Tell that to "+subscriber.login)
				msg_q_ws.append((msg,subscriber))


		#### confirm that is changed the state of detection or tell us that there is movement, for M2M
		elif(enc.get("cmd")=="state_change"):
			m2m.state=enc.get("state",4)
			m2m.detection=enc.get("detection",-1)
			
			# prepare notification system, arm or disarm
			if(m2m.state==1 and m2m.detection>=1): # state=1 means Alert!
				#start_new_alert(m2m)
				m2m.alert.notification_send=0 
				m2m.alert.collecting=1
				m2m.alert.id=db.create_alert(m2m,rm.get_account(m2m.account).get_area(m2m.area).print_rules(bars=0,account_info=0,print_out=0))
				m2m.alert.ts=time.time()
				m2m.alert.files = []
				m2m.alert.notification_send_ts = -1 # indicates that this is a thing to be done
				m2m.alert.last_upload=0
				
			elif(m2m.detection==0): #state 2 or 3 means: offline (+idle/+alert)
				# assuming that the system was already triggered and right after that the switch off command arrived -> avoid notification
				# check_alerts will search for m2m with notification_send_ts==-1
				m2m.alert.notification_send_ts = 0 # indicate that this is done

			# prepare messages
			# tell subscribers
			msg={}
			#res=db.get_open_alerts(m2m.account,0)
			#for key, value in res:
			#	msg[key]=value
			
			msg["mid"]=m2m.mid
			msg["cmd"]=enc.get("cmd")
			msg["state"]=m2m.state
			msg["area"]=m2m.area
			msg["account"]=m2m.account
			msg["detection"]=m2m.detection
			msg["rm"]=rm.get_account(m2m.account).get_area(m2m.area).print_rules(bars=0,account_info=0,print_out=0)
			informed=0
			for subscriber in m2m.m2v:
				msg_q_ws.append((msg,subscriber))
				informed+=1

			# print on console
			p.change_state(m2m,informed)



		#### wf -> write file, message shall send the fn -> filename and set the EOF -> 1 if it is the last piece of the file , for M2M
		elif(enc.get("cmd")=="wf"):
			# handle new file
			if(m2m.openfile!=enc.get("fn")):
				if(m2m.fp!=""):
					try:
						m2m.fp.close()
					except:
						m2m.fp=""
				m2m.openfile = enc.get("fn")
				base_location=str(int(time.time()))+"_"+m2m.mid+"_"+m2m.openfile
				des_location="../webserver/upload/"+base_location
				m2m.fp = open(des_location,'wb')
				# this is the start of a transmission
				# a client in ALERT state will send UP TO N pictures, but might be disconnected before he finished.
				# we'll put every alert file filename in the m2m.alert_img list  and check in the loop if that list
				# has reached 5 pics, or hasn't been updated for > 20 sec.
				# if those conditions are satifies we'll check if the mail optioin is active and if so mail it to
				# the given address. after that we set the m2m.alert_mail_send to 1 state change to low should clear that
				if(m2m.state==1 and m2m.detection>=1): # ALERT
					if(m2m.alert.collecting==1 and m2m.alert.notification_send==0): # not yet send, append fn to list and save timestamp
						db.append_alert_photo(m2m,base_location)
						m2m.alert.files.append(des_location)
						m2m.alert.last_upload = time.time()
					elif(m2m.detection==2): # if detection = permanant fire, then we're going to save the picture, even after fireing the mail
						db.append_alert_photo(m2m,des_location)
				#tmp_loc=des_location.split('/')
				#print("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+m2m.mid+"' uploads "+tmp_loc[len(tmp_loc)-1])
				m2m.paket_count_per_file=0

			# write this file
			m2m.fp.write(base64.b64decode(enc.get("data").encode('UTF-8')))

			# check if this packet contained the end of file
			if(enc.get("eof")==1):
				# end of file, close it
				this_file=m2m.fp.name  #store the name just in case we have to read it again
				try:
					m2m.fp.close()
				except:
					m2m.fp=""
				m2m.openfile=""	

				# prepare client message
				msg={}
				msg["mid"]=m2m.mid
				msg["cmd"]="rf"
				msg["state"]=m2m.state
				msg["area"]=m2m.area
				msg["detection"]=m2m.detection

				# all image data in one packet
				if(enc.get("sof",0)==1):
					#send img, assuming this is a at once img
					msg["img"]=enc.get("data")
					msg["ts"]=(enc.get("td"))[0][0]
				else:
					#read img and send at once, close this file pointer as it is writing only
					try:
						m2m.fp = open(this_file,'rb')
						msg["img"]=m2m.fp.read()
						m2m.fp.close()
					except:
						m2m.fp=""
					
				# select the ws to send to
				if(m2m.state==1 and m2m.detection>=1): # alert -> inform everyone
					# the m2v list has all viewer
					for v in m2m.m2v:
						if(v.snd_q_len<10 and v.alarm_view==1): # just send it if their queue is not to full AND the clients wants unrequested img
							msg_q_ws.append((msg,v))
							v.snd_q_len+=1
					#if(m2m.detection==1 and m2m.alert.notification_send_ts>0):		# TODO: if this is activated only the first xx file will be saved for detection=1 clients, detection=2 clients will save forever
					#	os.remove(this_file)
				# webcam -> use webcam list as the m2v list has all viewer, but the webcam has those who have requested the feed
				for v in m2m.webcam:
					#only update if last ts war more then interval ago
					ts_photo=enc.get("td",0) # td tells us when this photo was taken
					ts_photo=ts_photo[1][0]
					t_passed=ts_photo-v.ts+0.1
					if(t_passed>=v.interval and v.ws.snd_q_len<10 and v.ws.webcam_countdown>=1): # send only if queue is not too full
						v.ts=ts_photo
						v.ws.snd_q_len+=1
						v.ws.webcam_countdown-=1
						msg["webcam_countdown"]=v.ws.webcam_countdown
						msg_q_ws.append((msg,v.ws))
					elif(v.ws.webcam_countdown<1):
						set_webcam_con(m2m.mid,0,v.ws) # disconnect the webcam from us						
					else:
						p.rint("skipping "+str(v.ws.login)+": "+str(t_passed)+" / "+str(v.ws.snd_q_len),"u")
				if(m2m.detection==0):
					#  delete the picture from our memory, as it can not be a alert picture
					os.remove(this_file)

				tmp_loc=this_file.split('/')
				p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+m2m.mid+"' uploaded "+tmp_loc[len(tmp_loc)-1],"u")
				# send good ack
			if(enc.get("ack")==-1):
				msg={}
				msg["cmd"]=enc.get("cmd")
				msg["fn"]=enc.get("fn")
				msg["ok"]=1
				msg_q_m2m.append((msg,m2m))

			m2m.paket_count_per_file+=1


		#### unsupported command, for M2M
		else:
			p.rint("unsupported command: "+enc.get("cmd"),"d")
		#********* msg handling **************#
	#### comm error , for M2M
	else:
		p.rint("-d--> json decode error","d")
		msg={}
		msg["cmd"]=enc.get("cmd")
		msg["ok"]=-1 #comm error
		msg_q_m2m.append((msg,m2m))
#******************************************************#

#******************************************************#
# last but not least we have to send messages to the M2M
# this is done by the snd_m2m_msg_dq_handle. it will check if there
# is a message and forward it to the server
def snd_m2m_msg_dq_handle():
	ret=0
	if(len(msg_q_m2m)>0):
		ret=1
		#print(str(time.time())+' fire in the hole')
		data=msg_q_m2m[0]
		msg_q_m2m.remove(data)

		msg=data[0]
		m2m=data[1]
		if(0!=server_m2m.send_data(m2m,json.dumps(msg).encode("UTF-8"))):
			# the cam box m2m unit is not longer available .. obviously, remove it from every viewer and inform them
			recv_m2m_con_handle("disconnect",m2m)
	return ret
#******************************************************#
#***************************************************************************************#
#************************************** end of m2m *************************************#
#***************************************************************************************#


#***************************************************************************************#
#************************************** WebSockets *************************************#
#***************************************************************************************#
# introduction text required

#******************************************************#
# the websocket server will call this funcion to put new incoming connection in the queue
def recv_ws_con_q_handle(data,ws):
	recv_ws_con_q.append((data,ws))
#******************************************************#

#******************************************************#
# the main loop will call the dequeue to check if there are new connection changes
def recv_ws_con_dq_handle():
	ret=0
	if(len(recv_ws_con_q)>0):
		ret=1
		recv_con=recv_ws_con_q[0]
		recv_ws_con_q.remove(recv_con)
		recv_ws_con_handle(recv_con[0],recv_con[1])
	return ret
#******************************************************#

#******************************************************#
# dequeue above will call us to process the new connection
def recv_ws_con_handle(data,ws):
	# this function is is used to be callen if a ws disconnects, we have to update all m2m clients and their webcam lists
	#print("[A_ws "+time.strftime("%H:%M:%S")+"] connection change")
	if(data=="disconnect"):
		p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] WS disconneted","l")
		# try to find that websockets in all client lists, so go through all clients and their lists
		for m2m in server_m2m.clients:
			for viewer in m2m.m2v:
				if(viewer==ws):
					p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] releasing '"+m2m.mid+"' from "+ws.login,"l")
					m2m.m2v.remove(viewer)

					# also check if that ws has been one of the watchers of the webfeed
					set_webcam_con(m2m.mid,0,ws)

		try:
			server_ws.clients.remove(ws)
		except:
			ignore=1
#******************************************************#

#******************************************************#
# websocket server will call this function if new websocket messages arrived
# we will store them in the queue
def recv_ws_msg_q_handle(data,ws):
	recv_ws_msg_q.append((data,ws));
#******************************************************#

#******************************************************#
# and the main loop will call dequeue them if there are any and call msg_handle
def recv_ws_msg_dq_handle():
	ret=0
	if(len(recv_ws_msg_q)>0):
		ret=1
		recv_msg=recv_ws_msg_q[0]
		recv_ws_msg_q.remove(recv_msg)
		recv_ws_msg_handle(recv_msg[0],recv_msg[1])
	return ret
#******************************************************#

#******************************************************#
# callen by the dequeue above
def recv_ws_msg_handle(data,ws):
	global db
	try:
		enc=json.loads(data)
	except:
		enc=""
		p.rint("-d--> json decoding failed on:" + data,"d")

		#print("ws:"+str(ws.port)+"/"+str(ws.ip))
	if(type(enc) is dict):
		if(enc.get("debug",0)==1):
			p.rint("websocket_msg","d")
			for key, value in enc.items() :
				p.rint("-d-->Key:'"+key+"' / Value:'"+str(value)+"'","d")

		#### pre login challange, for WS
		elif(enc.get("cmd")=="prelogin"):
			ws.challange=get_challange()
			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["challange"]=ws.challange
			msg_q_ws.append((msg,ws))
			#print("received prelogin request, sending challange "+m2m.challange)

		## LOGIN from a viewer, for WS
		elif(enc.get("cmd")=="login"):
			msg_ws={}
			msg_ws["cmd"]=enc.get("cmd")
			# data base has to give us this values
			ws.login=enc.get("login")
			
			# database output
			pw2="124"
			h = hashlib.new('ripemd160')
			h.update(pw2.encode("UTF-8"))
			db_f={}
			db_f["pw"]=h.hexdigest() # todo get those data from sql
			db_f["account"]="jkw"
			# database output

			# check parameter
			if(db_f["pw"]==enc.get("client_pw") or enc.get("client_pw")=="hui"): # <<-- TODO, that is no very safe ;D
				# complete message
				msg_ws["ok"]=1 # logged in
				msg_q_ws.append((msg_ws,ws))
				
				# add socket infos
				ws.logged_in=1
				ws.account=db_f["account"]
				ws.last_comm=time.time()
				ws.uuid=enc.get("uuid","")
				ws.alarm_view=enc.get("alarm_view",0)
				
				# print and update db, as this fails when the client already disconnected, surrond with try catch
				p.ws_login(ws)
				try:
					ip=ws.conn.getpeername()[0]
				except:
					ip="???"
				db.update_last_seen_ws(ws.login, ip)
				
				# search for all (active and logged-in) camera modules with the same account and tell them that we'd like to be updated
				# introduce them to each other
				for m2m in server_m2m.clients:
					if(m2m.account==ws.account):
						connect_ws_m2m(m2m,ws)
				# and finally connect all disconnected m2m to the ws
				connect_ws_m2m("",ws)
				
				# check if the same UUID has another open connection
				if(ws.uuid!=""):
					for cli_ws in server_ws.clients:
						if((cli_ws.uuid==ws.uuid and cli_ws!=ws and cli_ws.login==ws.login) or (cli_ws.logged_in!=1 and time.time()-cli_ws.last_comm>10*60))  :
							#print("disconnecting "+str(cli_ws.login)+" IP "+str(cli_ws.ip)+" as that has the same UUID")
							cli_ws.conn.close()
							recv_ws_con_handle("disconnect", cli_ws)
				
			else:
				try:
					ip=ws.conn.getpeername()[0]
				except:
					ip="???"
				p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] log-in from "+ip+" failed for login '"+ws.login+"', password not correct","l")
				msg_ws["ok"]=-2 # not logged in
				msg_q_ws.append((msg_ws,ws))

		#### refresh, this will be called by the app, on a start. the service is already connected to us, so no need for a complete reconnect, but the app will need an update about all clients
		elif(enc.get("cmd")=="refresh_ws"):
			 # search for all (active and logged-in) camera modules with the same account and tell them that we'd like to be updated
			# introduce them to each other
			for m2m in server_m2m.clients:
				if(m2m.account==ws.account):
					connect_ws_m2m(m2m,ws,0) # call with 0 will avoid that we append us to a list
			# and finally connect all disconnected m2m to the ws
			connect_ws_m2m("",ws,0) # call with 0 will avoid that we append us to a list

		#### heartbeat, for WS
		elif(enc.get("cmd")=="ws_hb"):
			# respond
			p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+ws.login+"'@'"+ws.account+"' HB","h")
			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["ok"]=1
			msg_q_ws.append((msg,ws))
			ws.last_comm=time.time()


		#### set a color 
		elif(enc.get("cmd")=="set_color"):
			# respond
			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["r"]=enc.get("r")
			msg["g"]=enc.get("g")
			msg["b"]=enc.get("b")
			for m2m in ws.v2m:
				if(enc.get("mid")==m2m.mid):
					p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+ws.login+"' change color","v")
					db.update_color(m2m,int(enc.get("r")),int(enc.get("g")),int(enc.get("b")),int(enc.get("brightness_pos")),int(enc.get("color_pos")))
					
					m2m.color_pos=int(enc.get("color_pos"))
					m2m.brightness_pos=int(enc.get("brightness_pos"))
					msg_q_m2m.append((msg,m2m))
					break

		## Detection on/off handle, for WS --> this should be obsolete as the server can decide on its own when to activate the detection
		elif(enc.get("cmd")=="set_override"):
			area=enc.get("area")
			rule=enc.get("rule") # can be "*" for on or "/" for off
			duration=int(enc.get("duration"))
			p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+ws.login+"' sets a override '"+rule+"' for area '"+area+"'","v")
			
			r=rm.get_account(ws.account)
			if(r!=0):
				rule_area=r.get_area(area)
				if(rule_area!=0):
					# check if opposit rule existed and remove up front
					if(rule=="*" and rule_area.has_override_detection_off):
						rule_area.rm_override("/")
					elif(rule=="/" and rule_area.has_override_detection_on):
						rule_area.rm_override("*")
					else:	
						if(duration>0): # duration can be a time in sec or -1 = forever
							duration=int(time.time()+duration)
						rule_area.append_rule(rule,duration,0)	
				
				# update next timestamp (the override could be timelimited) and run a rulecheck as the szenario has most certainly changed		
				rm.get_account(ws.account).update_next_ts()
				rm_check_rules(ws.account,ws.login,1)
			else:
				#todo return bad ack
				ignore=1
			
		## webcam interval -> sign in or out to webcam, for WS
		elif(enc.get("cmd")=="set_interval"):
			set_webcam_con(enc.get("mid"),enc.get("interval",0),ws)

		## if a ws client supports location grabbing it can send location updates to switch on/off the detection, for WS
		elif(enc.get("cmd")=="update_location"):
			ws.location=enc.get("loc","")
			p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+ws.login+"'@'"+ws.account+"' moved to '"+enc.get("loc")+"'","h")
			# step 1: update database location for this login
			db_r=db.update_location(ws.login,enc.get("loc"))
			# step 2: run all rule checks and update every box on the account
			t=time.time()
			p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] checking as somebody moved for this account","r")
			rm_check_rules(ws.account,ws.login,1)	# check and use db
			p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] Check took "+str(time.time()-t),"r")

		## get IDs of open alerts
		elif(enc.get("cmd")=="get_alert_ids"):
			mid=enc.get("mid")
			open_start=enc.get("open_start",0)
			open_end=enc.get("open_end",10)
			closed_start=enc.get("closed_start",0)
			closed_end=enc.get("closed_end",10)

			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["ids_open"]=[]
			msg["ids_closed"]=[]
			msg["mid"]=mid
			msg["open_max"]=db.get_open_alert_count(ws.account, mid)
			msg["closed_max"]=db.get_closed_alert_count(ws.account, mid)

			db_r=db.get_open_alert_ids(ws.account,mid,open_start,open_end)
			if(db_r==-1):
				msg["ids_open"].append(-1)
			else:
				for i in db_r:
					msg["ids_open"].append(i['id'])

			db_r=db.get_closed_alert_ids(ws.account,mid,closed_start,closed_end)
			if(db_r==-1):
				msg["ids_closed"].append(-1)
			else:
				for i in db_r:
					msg["ids_closed"].append(i['id'])
			msg_q_ws.append((msg,ws))
					
		## get Details to a alarm ID
		elif(enc.get("cmd")=="get_alarm_details"):
			id=enc.get("id")
			if(id!=-1):
				p.rint("[A_WS  "+time.strftime("%H:%M:%S")+"] Received request for alarm: "+str(id)+" details","v")
				db_r1=db.get_alert_details(ws.account,id)
				## get number of pictures for this alert
				db_r2=db.get_img_count_for_alerts(id)
				## get picture path for 0..100 
				db_r3=db.get_img_for_alerts(id,0)
				msg={}
				msg["cmd"]=enc.get("cmd")
				msg["id"]=id
				msg["rm_string"]=db_r1['rm_string']
				msg["f_ts"]=db_r1['f_ts']
				msg["img_count"]=db_r2
				msg["img"]=db_r3
				msg["mid"]=enc.get("mid")		
				msg["ack"]=db_r1['ack']
				msg["ack_ts"]=db_r1['ack_ts']
				msg["ack_by"]=db_r1['ack_by']
				msg_q_ws.append((msg,ws))

		## reset webcam_countdown
		elif(enc.get("cmd")=="reset_webcam_countdown"):
			ws.webcam_countdown=99
			p.rint("[A_WS  "+time.strftime("%H:%M:%S")+"] Received countdown reset from "+ws.login,"v") 

		## get picture 
		elif(enc.get("cmd")=="get_img"):
			path=enc.get("path")
			p.rint("[A_WS  "+time.strftime("%H:%M:%S")+"] Received request for img: "+path+", uploading","v")
			db_r=db.get_account_for_path(path)
			if(db_r==ws.account):
				msg={}
				msg["cmd"]="recv_req_file"
				msg["path"]=path
				msg["height"]=enc.get("height")
				msg["width"]=enc.get("width")

				try:
					img = open("../webserver/upload/"+path,'rb')
				except:
					img = open("../webserver/images/filenotfound.jpg",'rb')
					
				strng=img.read(512000-100)
				img.close()
				msg["img"]=base64.b64encode(strng).decode('utf-8')
				msg_q_ws.append((msg,ws))

		# acknowledge alerts
		elif(enc.get("cmd")=="ack_alert" or enc.get("cmd")=="ack_all_alert"):
			if(enc.get("cmd")=="ack_alert"):
				db.ack_alert(enc.get("mid"),enc.get("aid"),ws.login)
			elif(enc.get("cmd")=="ack_all_alert"):
				db.ack_all_alert(enc.get("mid"),ws.login)
			msg={}
			msg["cmd"]="update_open_alerts"
			msg["mid"]=enc.get("mid")
			msg["open_alarms"]=db.get_open_alert_count(ws.account,enc.get("mid"))
			for cam in server_m2m.clients:
				if(cam.mid==enc.get("mid")):
					for v in cam.m2v:
						msg_q_ws.append((msg,v))
					break

		

		## unsupported cmd, for WS
		else:
			p.rint("[A ws  "+time.strftime("%H:%M:%S")+"] unsupported command: "+enc.get("cmd")+ " from "+ws.login,"d")
#******************************************************#

#******************************************************#
# and here again: the main loop will call us to check if there is a message to send back
def snd_ws_msg_dq_handle():
	ret=0
	if(len(msg_q_ws)>0):
		ret=1
		#print(str(time.time())+' fire in the hole')
		data=msg_q_ws[0]
		msg=data[0]
		cli=data[1]
		#try to submit the data to the websocket client, if that fails, remove that client.. and maybe tell him
		msg_q_ws.remove(data)
		cli.snd_q_len=max(0,cli.snd_q_len-1)
		if(server_ws.send_data(cli,json.dumps(msg).encode("UTF-8"))!=0):
			recv_ws_con_handle("disconnect",cli)
	return ret
#******************************************************#
#***************************************************************************************#
#*********************************** End of WebSockets *********************************#
#***************************************************************************************#

#***************************************************************************************#
#**************************************** Common  **************************************#
#***************************************************************************************#
# common function more than one of ws or m2m uses

#******************************************************#
# when ever a websocket or a m2m device signs-on this function will be called
# the purpose is that the web socket shall be informed about the new, available client
# and the m2m shall know that there is a viewer to inform
def connect_ws_m2m(m2m,ws,update_m2m=1):
	global rm
	if(m2m!=""): # first lets assume that we shall connect a given pair right here
		# add us to their (machine to viewer) list, to be notified whats going on
		if(update_m2m):
			if not(ws in m2m.m2v):
				m2m.m2v.append(ws)
			# and add them to us to give us the change to tell them if they should be sharp or not
			if not(m2m in ws.v2m):
				ws.v2m.append(m2m)
		# send a nice and shiny message to the viewer to tell him what boxes are online,
		p.connect_ws_m2m(m2m,ws)
		msg_ws2={}
		msg_ws2["cmd"]="m2v_login"
		msg_ws2["mid"]=m2m.mid
		msg_ws2["area"]=m2m.area
		msg_ws2["longitude"]=m2m.longitude
		msg_ws2["latitude"]=m2m.latitude
		msg_ws2["state"]=m2m.state
		msg_ws2["detection"]=m2m.detection
		msg_ws2["account"]=m2m.account
		msg_ws2["alias"]=m2m.alias
		msg_ws2["last_seen"]=m2m.last_comm
		msg_ws2["color_pos"]=m2m.color_pos
		msg_ws2["brightness_pos"]=m2m.brightness_pos
		msg_ws2["rm"]=rm.get_account(m2m.account).get_area(m2m.area).print_rules(bars=0,account_info=0,print_out=0)
		msg_ws2["rm_override_on"]=rm.get_account(m2m.account).get_area(m2m.area).has_override_detection_on
		msg_ws2["rm_override_off"]=rm.get_account(m2m.account).get_area(m2m.area).has_override_detection_off
		msg_ws2["open_alarms"]=db.get_open_alert_count(m2m.account,m2m.mid)
		msg_q_ws.append((msg_ws2,ws))
	else: # this will be called at the very end of a websocket sign-on, it shall add all non connected boxes to the websocket.
		# 1. get all boxed with the same account
		all_m2m4account=db.get_m2m4account(ws.account)
		if(type(all_m2m4account) is int):
			p.rint("Error getting data for account "+ws.account,"d")
		else:
			# 2. loop through them and make sure that they are not part of the list, that the ws already knows
			for m2m in all_m2m4account:
				found=0
				for am2m in ws.v2m:
					if(m2m["mid"]==am2m.mid):
						found=1
						break
				if(not(found)):
					# get the state from the DB for this box, eventhough it is not online
					db_r2=db.get_state(m2m["area"],ws.account)
					if(type(db_r2)!=int):
						detection=int(db_r2["state"])
					else:
						detection=-1

					msg_ws2={}
					msg_ws2["cmd"]="m2v_login"
					msg_ws2["mid"]=m2m["mid"]
					msg_ws2["area"]=m2m["area"]
					msg_ws2["alias"]=m2m["alias"]
					msg_ws2["longitude"]=m2m["longitude"]
					msg_ws2["latitude"]=m2m["latitude"]
					msg_ws2["state"]=-1
					msg_ws2["detection"]=detection
					msg_ws2["account"]=ws.account
					msg_ws2["last_seen"]=m2m["last_seen"]
					msg_ws2["color_pos"]=m2m["color_pos"]
					msg_ws2["brightness_pos"]=m2m["brightness_pos"]
					msg_ws2["rm"]="offline"
					msg_ws2["open_alarms"]=db.get_open_alert_count(ws.account,m2m["mid"])
			# 3. send data to the websocket
					msg_q_ws.append((msg_ws2,ws))
#******************************************************#

#******************************************************#
# this will be called if the websocket requests a webcam stream OR if he had done it before and disconnects
# purpose of this function is to ADD or REMOVE the websocket to the list "webcam" of the m2m unit and to tell
# the cam at what speed it shall run. BTW: there is a problem with the KGV .. but not important.
def set_webcam_con(mid,interval,ws):
	#print("--> change interval "+str(interval))
	msg={}
	msg["cmd"]="set_interval"
	#search for the m2m module that shall upload the picture to the ws

	for m2m in ws.v2m:
		if(m2m.mid==mid):
			#print("habe die angeforderte MID in der clienten liste vom ws gefunden")
			# thats our m2m cam
			if(interval>0):
				# reset counter
				ws.webcam_countdown=99;

				# scan if we are already in the webcam list, and remove us if so
				for wcv in m2m.webcam:
					if(wcv.ws==ws):
						m2m.webcam.remove(wcv)

				# put the ws on his list of webcam subscripters
				viewer=webcam_viewer(ws)
				viewer.interval=interval
				viewer.ts=0 # deliver the next frame asap
				m2m.webcam.append(viewer) 

				# find fastest webcam viewer
				sm_interval=9999
				for wcv in m2m.webcam:
					if(wcv.interval<interval):
						sm_interval=wcv.interval
				#check if our interval is even faster
				if(sm_interval>interval):
					#we requested a faster rate than every one else
					msg["interval"]=interval
					# inform the webcam that we are watching
					msg_q_m2m.append((msg,m2m))

				p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] Added "+ws.login+" to webcam stream from "+mid,"c")
					
			# this clients switched off 
			else:
				# remove us from the list 
				# go through all elements in the webcam list 
				#print("suche in der webcam liste nach unserem ws")
				for viewer in m2m.webcam:
					if(viewer.ws==ws):
						#print("gefunden und entfernt")
						m2m.webcam.remove(viewer)

						# check if we shall switch of the feed
						clients_remaining=len(m2m.webcam)
						if(clients_remaining==0):
							msg["interval"]=0
							#print("sende stop nachricht an m2m:"+str(m2m.mid))
						else:
							#find fastest webcam viewer
							sm_interval=9999
							for wcv in m2m.webcam:
								if(wcv.interval<sm_interval):
									sm_interval=wcv.interval
							msg["interval"]=sm_interval

						msg_q_m2m.append((msg,m2m))
						p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] Removed "+ws.login+" from webcam stream of "+m2m.mid+" ("+str(clients_remaining)+" ws left)","c")
#******************************************************#

#******************************************************#
# get_challange is used to avoid that the clients send their password or even an encripted version of it.
# first they request a challange (generated by this function) which will be send back to the client, than
# the client will append his password to it and encode that. this makes sure that the loginpw is different
# at all logins and nobody can just repeat the login sequence
def get_challange(size=12, chars=string.ascii_uppercase + string.digits):
	return ''.join(random.choice(chars) for _ in range(size))
#******************************************************#

#******************************************************#
# this function will be called in the main loop and shall check if there is a client in the state that he
# started to capture a few images and might be ready to send them via mail / notification
# TODO DAS KLAPPT DOCH NOCH NICHT HIER!
def check_alerts():
	ret=-1
	for cli in server_m2m.clients:
		if(cli.alert.notification_send==0 and cli.alert.collecting==1):
		#if(cli.alert.notification_send_ts==-1): # -1 = we switch to alert, we haven't switched back to "no alert" otherwise send_ts=0 and we haven't send the mail for this alert, otherwise this would be a timestamp
			# found client in "alert but not yet notified" state, see if it is time to notify
			send = 0
			# if the gab between the last_upload and now is > timeout, last_upload will be set every time a file arrives, and initialized to 0 once the state changes to alert
			if(cli.alert.last_upload!=0):
				if(time.time()>cli.alert.last_upload+cli.alert.file_max_timeout_ms/1000):
					#print("last upload ist old enough")
					send=1
			# or enough pictures have been uploaded
			if(len(cli.alert.files)>=cli.alert.files_expected):
				#print("found enough files")
				send=1

			# fire in the hole
			if(send==1):
				# email?
				if(cli.alert.comm_path % 2 == 1):
					#print("sending mail")
					#send_mail.send( subject, text, files=[], send_to="KKoolljjaa@gmail.com",send_from="koljasspam493@gmail.com", server="localhost"):
					# send a mail
					send_mail.send("alert", "oho", cli.alert.files)

					# send a notification to all clients
					msg={}
					msg["cmd"]="update_open_alerts"
					msg["mid"]=cli.mid
					msg["open_alarms"]=db.get_open_alert_count(cli.account,cli.mid)
					for viewer in cli.m2v:
						msg_q_ws.append((msg,viewer))


					cli.alert.notification_send_ts=time.time()
					p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+str(cli.mid)+"' triggered Email","a")
					ret=0
					cli.alert.collecting=0
					cli.alert.notification_send=1
	return ret
#******************************************************#

#******************************************************#
# this fuction shall be called if the environment changes. E.g. if a ws client change the location.
# it will load the rule_account from the rule manager by  the given "account" string and go through all areas,
# associated the this account. it will evaluate the rules for each area of the account and write the status to the database.
# after that it will go through the complete m2m_client list and grab every box that has the same accont, reload the status
# of the box from the database and send it to the box.
# the login argument is used to keep track what ws client has triggered all that changes
def rm_check_rules(account,login,use_db):
	global rm
	global db

	#print("### rm debug ###")
	#rm.print_all()
	#print("### rm debug ###")

	# get account from rulemanager
	acc=rm.get_account(account)
	if(acc!=-1):
		# run the rule check for every area in this account
		#print("running rule check on every area of this account")
		for b in acc.areas:
			detection_state=b.check_rules(use_db) 	# get the rule state, 1 for detection on and 0 for off ... this is NOT the detection state the box shall get (could be 2)
			if(detection_state): # if the alert should be "on", grab the first box you can find in this account and area and check what the detection_on_mode is to set it to 1 or 2
				real_detection_state=1 # backup
				for m2m in server_m2m.clients:
					if(m2m.account==account and m2m.area==b.area):
						real_detection_state=m2m.detection_on_mode
						break
			else:
				real_detection_state=0
			db.update_det(login,account,b.area,real_detection_state)
			#print("updateing to db that detection of area "+str(b.area)+" should be")
			#print(detection_state)

		# send an update to every box in this account ## TODO: only the boxes which changed the status?
		#print("now we have to check for every box what there detection status shall be and send it to them")
		for m2m in server_m2m.clients:
			if(m2m.account==account):
				#print("checkin for box "+m2m.alias+" in area "+m2m.area)
				db_r2=db.get_state(m2m.area,account)
				if(type(db_r2)!=int):
					#print("will I send that detection state should be "+str(db_r2["state"])+"?")
					#print("because m2m.detection is: "+str(m2m.detection))
					detection=int(db_r2["state"])
					if(m2m.detection!=detection):
						m2m.detection=detection
						msg={}
						msg["cmd"]="set_detection"
						msg["state"]=detection
						# step 8 append message for this m2m client to go sharp ;)
						msg_q_m2m.append((msg,m2m))
						# step 9 tell the watching ws that it went sharp		
						affected_ws_clients=0
						for ws in m2m.m2v:
							msg2={}
							msg2["cmd"]="detection_changed"	
							msg2["area"]=m2m.area
							msg2["detection"]=m2m.detection
							msg_q_ws.append((msg2,ws))
							affected_ws_clients+=1
						p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] ->(M2M) set detection of m2m '"+m2m.mid+"' in area "+m2m.area+" to '"+str(det_state[int(db_r2["state"])])+"' (-> "+str(affected_ws_clients)+" ws clients)","a")
						#break DO NOT! MIGHT HAVE MULTIPLE BOXES

#******************************************************#

#******************************************************#
# this is the call back function that the p process uses whenever the user typed "ENTER". we'll react by putting some debug output on the terminal.
# as this code has all the variables we just call the Displaying function in p with our variables ..
def helper_output(input):
	print("")
	
	if(input=="rm"):
		p.show_m2m(1,0,0)
		rm.print_all()
		p.show_m2m(1,0,0)
	
	elif(input=="ws"):
		p.show_ws(-2,len(server_ws.clients),0)
		p.show_ws(-1,0,0)
		for ws in server_ws.clients:
			p.show_ws(0,0,ws)
		p.show_ws(1,0,0)

	elif(input=="m2m"):
		p.show_m2m(-2,len(server_m2m.clients),0)
		p.show_m2m(-1,0,0)
		for m2m in server_m2m.clients:
			p.show_m2m(0,0,m2m)
		p.show_m2m(1,0,0)
	
	else:
		print("whoot? ->"+input+"<-")
		print("your choices are:")
		print("m2m: to print informations about the connected camera clients")
		print("ws: to print informations about the connected websocket clients")
		print("rm: to print informations about the rule manager")

	print("")
#******************************************************#
#***************************************************************************************#
#************************************ End of Common  ***********************************#
#***************************************************************************************#

#***************************************************************************************#
#************************************** Variables **************************************#
#***************************************************************************************#

# our helper for the console
p.start()
p.subscribe_callback(helper_output)

# M2M structures
recv_m2m_msg_q=[]	# incoming
recv_m2m_con_q=[]	# incoming
msg_q_m2m=[]		# outgoing
server_m2m.start()
server_m2m.subscribe_callback(recv_m2m_msg_q_handle,"msg")
server_m2m.subscribe_callback(recv_m2m_con_q_handle,"con")

# WS structures
recv_ws_msg_q=[]	# incoming
recv_ws_con_q=[]	# incoming
msg_q_ws=[] 		# outgoing
server_ws.start()
server_ws.subscribe_callback(recv_ws_msg_q_handle,"msg")
server_ws.subscribe_callback(recv_ws_con_q_handle,"con")

# DB Structure used for the login
db=sql()
db.connect()

# our rule set maanger for all clients. Argument is the callback function
rm = rule_manager()


# else
busy=1
last_rulecheck_ts=0

#***************************************************************************************#
#********************************** End of Variables ***********************************#
#***************************************************************************************#

#***************************************************************************************#
#************************************** Main loop **************************************#
#***************************************************************************************#
last_ws_bh_time=0

while 1:
	# sleeping
	if(busy==0):
		time.sleep(0.03)
	busy=0

	############### recv ###################
	if(recv_m2m_con_dq_handle()==1): #returns 1 if there was a connection change by a m2m unit
		busy=1

	if(recv_m2m_msg_dq_handle()==1): #returns 1 if there was a message to receive from a m2m unit
		busy=1

	if(recv_ws_con_dq_handle()==1): #returns 1 if there was a connection change by a web socket client
		busy=1

	if(recv_ws_msg_dq_handle()==1):  #returns 1 if there was a message to receive from a web socket client
		busy=1

	############## send ###################
	if(snd_m2m_msg_dq_handle()==1): #returns 1 if there was a message to send
		busy=1

	if(snd_ws_msg_dq_handle()==1): #returns 1 if there was a message to send
		busy=1

	############## maintenance ###################
	# check if we have clients in the alert state ready to send a mail or so
	if(check_alerts()==0):
		busy=1

	# check the rules
	if(time.time()>last_rulecheck_ts+60): # this is not a good way .. we should know when we have to call it for a timebased change, not guess it
		busy=1
		last_rulecheck_ts=time.time()
		p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] running periodically 60 sec check","r")
		now=time.localtime()[3]*3600+time.localtime()[4]*60+time.localtime()[5]
		#print(time.localtime()[3]*3600+time.localtime()[4]*60+time.localtime()[5])
		for acc in rm.data:
			if(now>acc.next_ts or acc.check_day_jump()): # next_ts hold the time when a rule will change
				p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] full rule_check for account "+acc.account+" required","r")
				rm_check_rules(acc.account,"timetrigger",1) # check with database
				# reset next ts and check again, as this rule is "over"
				acc.next_ts=-1
				acc.update_next_ts()
		debug_ts=time.time()-last_rulecheck_ts
		p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] Check took "+str(debug_ts),"r")

			
#***************************************************************************************#
#********************************** End of Main loop ***********************************#
#***************************************************************************************#
	


					

