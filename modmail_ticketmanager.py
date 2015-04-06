# modmail_ticketmanager
# Python script developed on behalf on a need on /r/civcraft.
# Goal is to actively monitor the modmail queue on a particular subreddit
# 	and if a new modmail (or update) comes through, to push that into a
#	Request Tracker (ticket manager) instance.  
#
# Dependencies:  
#	Python
# 	PRAW - You will need to install this yourself. (pip install)
#	RequestTracker instance - You will need to install and configure this yourself.  (see http://requesttracker.wikia.com/wiki/DebianSqueezeInstallGuide )
#	Sqlite - You will need to install this yourself.  (apt-get)
# 	RequestTracker python helper rtkit (pip install)
#
# To use, change the relevant items in the #Definitions section.  You should not change
#	anything below that line.  When ran, this will put itself into a loop with waits in 
#	between and process modmails during its runtime.  This makes use of the fact that 
#	the latest changed modmail thread will be the first one to come through.  Using that
#	fact, we dont have to pull down all possible modmail threads each time - just enough
#	until we get to a thread that we have already fully processed.  If we get to this thread
#	then we are done for that iteration and can sleep for a bit.
#
#	Note:  This order-by behavior we are exploiting is NOT defined, so while likely to continue
#	in the future, we still have to get all messages every once in a while.  By default, thats 
#	every 30 minutes (configurable).  When that comes up, we will process all modmail messages 
#	with the newest reply > up to 8 days in the past (configureable).  
#
#	We keep track of items we have already processed by storing it in a sqlite database that you
#	define the name of.  It is expected that you will handle backing up this item on an intermittent
#	basis.  Woe unto those who choose not to do so as duplication in your ticket managing software
#	is the possible ramification if you do not.
#
#	It has not been tested what happens if you get a modmail update to something you mark DELETED
#	in the ticket management software, but since this deletion is soft deletion and not hard
#	it is expected that it will still be fine.

# Definitions - Change the items below.
debug = False #If set to True then will output a very large amount of data allowing you to debug what is going on.

# Reddit
redditUsername = ''
redditPassword = ''
redditSleepIntervalInSecondsBetweenRequests = 5
# The MinutesBetweenExtendedValidationMode and MaximumAmountOfDaysToAllowLookbackForMissingReplies are pretty tightly coupled concepts.
# Since we process things from newest to oldest, we are using a shortcut that lets us know when to quit (when we hit the first message 
#	that is already 100% processed we can end for now).  This is great for speeding up processing but horrible when you realize that
#	reddit fails quite a lot.  This means we could process the newest message but we havent processed messages after that leaving
#	those replies to be 'lost.'  This means every-so-often we need to look back over a good chunk of messages to make sure we have 
# 	everything we should.  We would automatically 'find' the messages if anything replied to the chain but if nothing does
#	they will be picked up in the extended validation.  Given the default for this is ~30 minutes, the lookback being a single day 
#	would suffice.  We are setting the default to 8 to allow a downtime interval.  This process can recover from downtime up to 
#	~7 ish days with this setting.  If we have a downtime event > 7 days then you either need to change this variable or accept
#	that some replies could be missing until someone touches that thread again.  Downtime > a week should be rare one would hope.
redditMinutesBetweenExtendedValidationMode = 30
redditMaximumAmountOfDaysToAllowLookbackForMissingReplies = 8 
redditSubredditToMonitor = '' # in text, like civcraft
# Explicit limiter on the number of modmails to pull.  This is the max you will ever get - you won't even see threads if they
#	exist beyond this limit.  Change this if you feel the need.  This is not -replies- in a thread but the master / root threads.
# 	A larger number will let you track more threads initially but this will slow you down for each processing cycle.  This should
#	be set just high enough for your uses and needs to be set by whoever owns a subreddit.
redditMaximumNumberOfRootThreadsToLookBack = 5000
redditAbsoluteOldestModmailRootNodeDateToConsider = 1420070400 # Epoch Notation for Jan 01 2015.  
															   # If you want to pull in tons and tons of history you could make this 0.

# SqlLite Information
sqliteDatabaseFilename = 'ModMailTicketManager.sqlite' # If this doesnt exist, it creates.
sqliteDatabaseTablename = 'HandledTickets' # TableName you wish to use for handled tickets.  We will create it.

# Request Tracker
requestTrackerRestApiUrl = 'http://192.168.25.129/rt/REST/1.0/' # Pretty much your url + /Rest/1.0/
requestTrackerQueueToPostTo = 1 # Tools -> Configuration -> Queues -> Select, whichever queue you wish.
# Request Tracker - User to use to post.
requestTrackerUsername = '' 
requestTrackerPassword = '' 

# End Definitions - Do not modify files below this line.

# Request Tracker Specific 
# https://github.com/z4r/python-rtkit#comment-on-a-ticket-with-attachments
from rtkit.resource import RTResource
from rtkit.authenticators import CookieAuthenticator
from rtkit.errors import RTResourceError

# Switched from BasicAuthenticator to CookieAuthenticator due to issues with basic auth.
# http://stackoverflow.com/questions/17890098/how-to-create-a-ticket-in-rt-using-python-rtkit
resource = RTResource(requestTrackerRestApiUrl, requestTrackerUsername, requestTrackerPassword, CookieAuthenticator)

# other
import praw
import time
import sqlite3
import sys, traceback
from datetime import datetime
from datetime import timedelta  
from pprint import pprint

prawUserAgent = 'ModMailTicketCreator v0.01 by /u/Pentom'

def init():
	global sqlConn
	global sqlCursor
	global nextExtendedValidationInterval
	
	sqlConn = None
	sqlCursor = None
	
	period = (datetime.now() + timedelta(minutes=redditMinutesBetweenExtendedValidationMode) - datetime(1970,1,1))
	nextExtendedValidationInterval = period.days * 1440 + period.seconds
	
	openSqlConnections()
	sql = 'CREATE TABLE IF NOT EXISTS ' + sqliteDatabaseTablename + '(CommentId TEXT PRIMARY KEY, ParentCommentId TEXT, TicketId INTEGER, CHECK((ParentCommentId is null and TicketId is not null) OR (ParentCommentId is not null and TicketId is null)));'
	sqlCursor.execute(sql)
	closeSqlConnections()
	openSqlConnections()
	sql = 'CREATE UNIQUE INDEX IF NOT EXISTS UQ_' + sqliteDatabaseTablename + '_ParentCommentId_CommentId ON ' + sqliteDatabaseTablename + '(ParentCommentId, CommentId);'
	sqlCursor.execute(sql)
	closeSqlConnections()
	setGlobalVariablesForExtendedValidationMode()
	
	
def openSqlConnections():
	global sqlConn
	global sqlCursor
	if sqlConn == None:
		sqlConn = sqlite3.connect(sqliteDatabaseFilename)
	if sqlCursor == None:
		sqlCursor = sqlConn.cursor()
	
def closeSqlConnections():
	global sqlConn
	global sqlCursor
	sqlConn.commit()
	sqlCursor.close()
	sqlConn.close()
	sqlCursor = None
	sqlConn = None
	
def processModMail():
	global nextExtendedValidationInterval
	openSqlConnections()
	
	try:
		r = praw.Reddit(user_agent=prawUserAgent)
		r.login(redditUsername,redditPassword)
		
		inExtendedValidationMode = False
		
		# see if its time to process in extended validation mode.
		period = (datetime.now() - datetime(1970,1,1))
		if (nextExtendedValidationInterval < (period.days * 1440 + period.seconds)):
			print('Processing in ExtendedValidationMode')
			setGlobalVariablesForExtendedValidationMode()
			period = (datetime.now() + timedelta(minutes=redditMinutesBetweenExtendedValidationMode) - datetime(1970,1,1))
			nextExtendedValidationInterval = period.days * 1440 + period.seconds
			inExtendedValidationMode = True
		
		if debug:
			print('Logged into Reddit.')

		sub = r.get_subreddit(redditSubredditToMonitor)
		for mail in sub.get_mod_mail(limit=redditMaximumNumberOfRootThreadsToLookBack):
			
			# When we are processing a message, we have the information to know if we should continue
			# processing.  This will keep returning true until we hit some message where we should hit falses.
			shouldContinueProcessing = processModMailRootMessage(debug, mail, inExtendedValidationMode)
			
			if not shouldContinueProcessing:
				break
	except:
		# Errors will happen here, Reddit fails all the time.
		# Do not vulgarly error out.
		e = str(sys.exc_info()[0])
		l = str(sys.exc_traceback.tb_lineno)
		error = str(datetime.utcnow()) + ' - Error when attempting to review modmail on line number ' + l + '.  Exception:  ' + e
		print(error)
		
		# Go overboard on logging if we are in debug mode.  
		if debug:
			exc_type, exc_value, exc_traceback = sys.exc_info()
			print "*** print_tb:"
			traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)
			print "*** print_exception:"
			traceback.print_exception(exc_type, exc_value, exc_traceback,
									  limit=2, file=sys.stdout)
			print "*** print_exc:"
			traceback.print_exc()
			print "*** format_exc, first and last line:"
			formatted_lines = traceback.format_exc().splitlines()
			print formatted_lines[0]
			print formatted_lines[-1]
			print "*** format_exception:"
			print repr(traceback.format_exception(exc_type, exc_value,
												  exc_traceback))
			print "*** extract_tb:"
			print repr(traceback.extract_tb(exc_traceback))
			print "*** format_tb:"
			print repr(traceback.format_tb(exc_traceback))
			print "*** tb_lineno:", exc_traceback.tb_lineno
			
		pass
	closeSqlConnections()
	openSqlConnections()
	
def shouldAnyMoreMessagesBeProcessed(wasMessageAlreadyFullyInSystem, newestMessageEpochTimeUtc, inExtendedValidationMode):
	# If the newest message is before our drop-dead oldest value, then we stop.  
	#	(redditAbsoluteOldestModmailRootNodeDateToConsider)
	# If the message is fully processed and its newest message is more than _x period_ old
	#	then we stop.  Why?  We are basically giving the max amount of time before we consider
	#   we give up looking.  Since we call this pretty blasted often, this would require an
	#	extended downtime period.  In that case, its on the devops staff to change this period
	#	if they need to recover from extended downtime.
	# Else continue.
	continueProcessing = True
	if newestMessageEpochTimeUtc < redditAbsoluteOldestModmailRootNodeDateToConsider:
		continueProcessing = False
	elif wasMessageAlreadyFullyInSystem and not inExtendedValidationMode:
		continueProcessing = False
	elif wasMessageAlreadyFullyInSystem and inExtendedValidationMode and newestMessageEpochTimeUtc < extendedValidationModeOldDatePeriod:
		continueProcessing = False
	
	return continueProcessing
	
# UTC vs Local date-time issue here I think.
# TODO:  Fix if we care.  For now, just push it out one more date.  
#			(no issue will be more than 12 hours so +24 and 'who cares for now')
# When called, will update our understanding of our extended period end date.
def setGlobalVariablesForExtendedValidationMode():
	global extendedValidationModeOldDatePeriod
	period = (datetime.now() - timedelta(days=8) - datetime(1970,1,1))
	extendedValidationModeOldDatePeriod = period.days * 1440 + period.seconds
	
	
def processModMailRootMessage(debug, mail, inExtendedValidationMode):
	shouldContinueProcessingMail = True
	alreadyProcessedAllItems = True
	
	openSqlConnections()
	#Helping debug output.
	firstTime = True
	debugText = ''

	if firstTime and debug:
		firstTime = False
		print('Found at least one item in modmail.')
	
	rootAge       = int(round(float(str(mail.created_utc))))
	rootAuthor    = str(mail.author)
	rootSubject   = str(mail.subject)
	rootBody      = str(mail.body)
	rootMessageId = str(mail.id) # Base 36, contains alphanumeric
	rootResponseUrl = 'http://www.reddit.com/message/messages/' + rootMessageId
	rootReplies   = mail.replies
	
	# track the newest age value amongst root and replies.
	messageNewestAge = rootAge
		
	if debug:
		debugText = 'Checking if core message is handled yet.  Subject:  ' + rootSubject
		print(debugText)
		
	# Has the current parent item been handled yet?  
	ticketId = getTicketIdForAlreadyProcessedRootMessage(rootMessageId)
	
	#If we dont find it, we need to add it in.
	if ticketId == None:
		alreadyProcessedAllItems = False #	There is at least one thing that we didnt find.
		
		if debug:
			print('Core message not found in system.  Processing.')
			
		ticketId = createTicket(rootAuthor, rootSubject, rootBody, rootResponseUrl)
		
		if debug:
			debugText = 'Added ticket to ticket system - ticket id:  ' + str(ticketId)
			print(debugText)
		
		if ticketId < 1:
			raise LookupError('Did not get back appropriate ticket id to store from ticket system')
		
		noteTheFactWeProcessedAMessageId(rootMessageId, None, ticketId)
	else:
		if debug:
			print('Core message found in system already.')
			
	if debug:
		print('Checking children that may exist.')
	
	# At this point, variable ticketId is the appropriate integer ticket number where the parent is already at.
	# Now that we have handled the parent, check for each of the children within this root parent.
	allRepliesHandled = handleMessageReplies(debug, ticketId, rootMessageId, rootReplies, messageNewestAge)
	alreadyProcessedAllItems = alreadyProcessedAllItems and allRepliesHandled
	
	shouldContinueProcessingMail = shouldAnyMoreMessagesBeProcessed(alreadyProcessedAllItems, messageNewestAge, inExtendedValidationMode)
	
	return shouldContinueProcessingMail
	
def noteTheFactWeProcessedAMessageId(messageId, parentMessageId, ticketId):
	sql = ''
	
	if parentMessageId == None:
		sql = 'INSERT INTO ' + sqliteDatabaseTablename + '(ParentCommentId, CommentId, TicketId) values (null, ?, ?);'
		sqlCursor.execute(sql, (messageId,ticketId))
	else:
		sql = 'INSERT INTO ' + sqliteDatabaseTablename + '(ParentCommentId, CommentId, TicketId) values (?, ?, null);'
		sqlCursor.execute(sql, (parentMessageId,messageId))
		
	
	sqlConn.commit()

def getHasReplyBeenProcessed(rootMessageId, replyMessageId):
	processed = True
	
	# Has the current child item been handled yet?  
	sql = 'select 1 from ' + sqliteDatabaseTablename + ' where ParentCommentId = ? and CommentId = ?;'
	sqlCursor.execute(sql, (rootMessageId,replyMessageId))   
	
	#If we dont find it, we need to add it in.
	sqlrow = sqlCursor.fetchone()
	if sqlrow == None:
		processed = False
	
	return processed
	
def getTicketIdForAlreadyProcessedRootMessage(rootMessageId):
	ticketId = None
	
	sql = 'select TicketId from ' + sqliteDatabaseTablename + ' where ParentCommentId is null and CommentId = ?;'
	sqlCursor.execute(sql, (rootMessageId,)) # [sic] you have to pass in a sequence.  
	
	sqlrow = sqlCursor.fetchone()
	if sqlrow != None:
		ticketId = sqlrow[0]
	
	return ticketId

# In reply object
# out - True/False - True iff all children found were ones we already processed
# NOTE:  messageNewestAge is being passed by reference and we are updating the value
# 	outside of this function.
def handleMessageReplies(debug, ticketId, rootMessageId, replies, messageNewestAge):
	firstTimeWithReply = True
	foundAllItems = True
	
	for reply in replies:
					
		if debug and firstTimeWithReply:
			firstTimeWithReply = False
			print('Found at least one reply to core message.')

		replyAuthor    = str(reply.author)
		replyBody      = str(reply.body)
		replyMessageId = str(reply.id) # Base 36, contains alphanumeric
		replyAge       = int(round(float(str(reply.created_utc))))
		
		# messageNewestAge is passed by reference, we are updating data in the core routine.
		if replyAge > messageNewestAge:
			if debug:
				debugText = 'Found a message component with a newer age.  Old lowest-age = ' + str(messageNewestAge) + ', New lowest-age = ' + str(replyAge)
				print(debugText)
			messageNewestAge = replyAge
		
		if debug:
			debugText = 'Checking if message reply is handled yet.  Body:  ' + replyBody
			print(debugText)
		
		# Has the current child item been handled yet?  
		alreadyProcessed = getHasReplyBeenProcessed(rootMessageId, replyMessageId)
		
		if not alreadyProcessed:
			foundAllItems = False #	There is at least one thing that we didnt find.
			
			if debug:
				print('Reply message not found in system.  Processing.')
		
			if debug:
				debugText = 'Updating ticket found in our system:  ' + str(ticketId)
				print(debugText)
			
			addTicketComment(ticketId, replyAuthor, replyBody)
			
			noteTheFactWeProcessedAMessageId(replyMessageId, rootMessageId, None)
		else:
			if debug:
				print('Reply message already found in system.')
	
	return foundAllItems
	
# no error handling, let errors bubble up.
# in - message information
# out integer ticket id.
def createTicket(author, subject, body, modmailMessageUrl):
	postedSubject = 'Modmail - ' + author + ' - ' + subject
	postedBody = 'Post from ' + author + '\nResponse URL: ' + modmailMessageUrl + '\nContents:\n' + body
	content = {
		'content': {
			'Queue': requestTrackerQueueToPostTo,
			'Subject': postedSubject,
			'Text': postedBody,
		}
	}
	response = resource.post(path='ticket/new', payload=content,)

	# if this wasnt successful, the following statements will error out and send us down to the catch.
	strTicket = (response.parsed[0][0][1]).split('/')[1]
	ticketId = int(strTicket)
	return ticketId
	
# no error handling, let errors bubble up.
# in - message information
# out None
def addTicketComment(ticketId, author, body):
	postedBody = 'Post from ' + author + '\nContents:\n' + body
	params = {
		'content': {
			'Action': 'comment',
			'Text': postedBody,
		}
	}
	ticketUpdatePath = 'ticket/' + str(ticketId) + '/comment'
	response = resource.post(path=ticketUpdatePath, payload=params,)
	
	# if this wasnt successful, the type will not be 200 and we will be sent down to the except.
	if response.status_int != 200:
		raise LookupError('Was unable to find/update expected ticket.')
	
def mainloop():
	
	while True:
		if debug:
			print('Waking... Processing modmail.');
			
		processModMail()
		
		if debug:
			print('Modmail processed.  Sleeping...');
			
		time.sleep(redditSleepIntervalInSecondsBetweenRequests); # sleep x seconds and do it again.

init()
mainloop()