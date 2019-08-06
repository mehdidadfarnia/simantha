from random import random

import numpy as np
import pandas as pd
import simpy

class Machine:
    '''
    Machine object. Processes discrete parts while not failed or under repair.
    '''
    def __init__(self, 
                 env, 
                 i, 
                 process_time,
                 planned_failures,
                 failure_mode,
                 failure_params,
                 initial_health,
                 system,
                 allow_new_maintenance):
        # system properties
        self.env = env
        self.system = system
        
        # machine properties
        self.i = i
        self.name = f'M{self.i}'
        self.process_time = process_time
        
        # failures and degradation
        self.planned_failures = planned_failures
        self.failure_mode = failure_mode
        if self.failure_mode == 'degradation': # Markov degradation
            self.degradation = failure_params
            self.failed_state = len(self.degradation) - 1

        # determine maintenance policy for machine
        self.maintenance_policy = self.system.maintenance_policy
        maintenance_parameters = self.system.maintenance_params
        if self.maintenance_policy == 'CBM':
            self.CBM_threshold = maintenance_parameters['CBM threshold'][self.i]
        else:
            self.maintenance_policy == 'CM'
        
        self.allow_new_maintenance = allow_new_maintenance

        # assign input buffer
        if self.i > 0:
            self.in_buff = self.system.buffers[self.i-1]
            
        # assign output buffer
        if (self.i < self.system.n-1):
            self.out_buff = self.system.buffers[self.i]
        
        # set initial machine state
        self.health = initial_health
        self.last_repair_time = None
        self.failed = False
        self.down = False
        self.time_entered_queue = 99999
        
        # set maintenance request state based on initial health
        if ((self.maintenance_policy == 'CBM') 
            and (self.CBM_threshold <= self.health < 10)):
            self.request_maintenance = True
            self.repair_type = 'CBM'
            self.time_entered_queue = self.env.now
        elif self.health == 10:
            self.request_maintenance = True
            self.repair_type = 'CM'
            self.failed = True
            self.time_entered_queue = self.env.now
        else:
            self.request_maintenance = False
            self.repair_type = None
        
        self.assigned_maintenance = False        
        self.under_repair = False
        
        # production state
        self.idle = True
        self.has_part = False
        self.remaining_process_time = self.process_time
        self.parts_made = 0
        self.total_downtime = 0 # blockage + startvation + repairs
        
        self.process = self.env.process(self.working())
        if self.failure_mode == 'degradation':
            # start Markovian degradation process
            self.failing = self.env.process(self.degrade())
        
        self.planned_downtime = self.env.process(self.scheduled_failures())

        #if self.system.debug:
        if 0:
            self.env.process(self.debug_process())
                
    def debug_process(self):
        while True:
            try:
                if (self.i == 0):
                    print(f't={self.env.now}', self.system.available_maintenance)
                    pass
                yield self.env.timeout(1)
                
            except simpy.Interrupt:
                pass

    def working(self):
        '''
        Main production function. Machine will process parts
        until interrupted by failure. 
        '''
        while True:
            try:                
                self.idle_start = self.idle_stop = self.env.now
                self.idle = True

                # get part from input buffer
                if self.i > 0:
                    yield self.in_buff.get(1)                    
                    self.system.state_data.loc[self.env.now, f'b{self.i-1} level'] = self.in_buff.level 

                    self.idle_stop = self.env.now

                self.has_part = True
                self.idle = False

                # check if machine was starved
                if self.idle_stop - self.idle_start > 0:                    
                    self.system.machine_data.loc[self.idle_start:self.idle_stop-1, 
                                                 self.name+' forced idle'] = 1

                    if self.env.now > self.system.warmup_time:       
                        self.total_downtime += (self.idle_stop - self.idle_start)
                        #if self.i==0: print(f'Incrementing idle time by {self.idle_stop - self.idle_start} at t={self.env.now}')
                # process part
                while self.remaining_process_time:
                    self.system.state_data.loc[self.env.now, self.name+' R(t)'] = self.remaining_process_time
                    self.system.machine_data.loc[self.env.now, self.name+' forced idle'] = 0
                    yield self.env.timeout(1)
                    
                    self.remaining_process_time -= 1

                # put finished part in output buffer
                self.idle_start = self.env.now
                self.idle = True
                if self.i < self.system.n-1:
                    yield self.out_buff.put(1) 
                    self.system.state_data.loc[self.env.now, f'b{self.i} level'] = self.out_buff.level                    

                    self.idle_stop = self.env.now
                    self.idle = False
                
                if self.env.now > self.system.warmup_time:
                    self.parts_made += 1
                    
                self.system.production_data.loc[self.env.now, f'M{self.i} production'] = self.parts_made
                
                self.has_part = False
                
                self.remaining_process_time = self.process_time

                # check if machine was blocked
                if self.idle_stop - self.idle_start > 0:                    
                    self.system.machine_data.loc[self.idle_start:self.idle_stop-1, 
                                                 self.name+' forced idle'] = 1
                    if self.env.now > self.system.warmup_time:
                        self.total_downtime += (self.idle_stop - self.idle_start)
                        #if self.i==0: print(f'Incrementing idle time by {self.idle_stop - self.idle_start} at t={self.env.now}')
                        #print(f'M{self.i} blocked from t={self.idle_start} to t={self.idle_stop}')
                                
            except simpy.Interrupt:
                if self.system.debug and self.i == 0: 
                    print('  '*self.i+f'M{self.i} interrupted production at t={self.env.now}')
                self.down = True

                #self.write_failure()

                # while not self.under_repair:
                #     #print(f'M{self.i} waiting for repair at t={self.env.now}')
                #     yield self.env.timeout(1)
                #print(f'M{self.i} under repair at t={self.env.now}')

                # wait for maintenance to finish
                yield self.env.process(self.maintain())

                # stop production until online
                while self.down:
                    yield self.env.timeout(1)
                    #if self.i == 3: print(f'M{self.i} down at t={self.env.now}')
                if self.system.debug and self.i == 0: 
                    print('  '*self.i+f'M{self.i} resumed production at t={self.env.now}\n')

    def maintain(self):
        #while True:
            
            # while not self.assigned_maintenance:
            #     # wait to be scheduled for maintenance
            #     if self.i == 0: print(f'M{self.i} checking if due for maintenance at t={self.env.now}')
            #     yield self.env.timeout(1)

        if self.system.debug and self.i == 0: 
            print('  '*self.i+f'M{self.i} scheduled for maintenance at t={self.env.now}')
        
        if self.repair_type == 'CBM':
            if not self.system.available_maintenance:
                return
            if self.system.debug and self.i == 0: 
                print('  '*self.i+f'M{self.i} scheduled for CBM at t={self.env.now}')
        elif self.repair_type == 'CM':
            while not self.system.available_maintenance:
                yield self.env.timeout(1)

        # break loop once scheduled for maintenance
        self.assigned_maintenance = False
        self.request_maintenance = False
        self.under_repair = True
        self.failing.interrupt() # stop degradation during maintenance
        self.system.available_maintenance -= 1 # occupy one maintenance resource

        downtime_start = self.env.now

        self.has_part = False

        # check if part was finished before failure occured
        # try:
        #     if (self.system.n > 1) and (self.system.state_data.loc[self.env.now-1, f'M{self.i} R(t)'] == 1):                    
        #         # I think this works. Might need further valifation
        #         if self.i == self.system.n-1:
        #             if self.env.now > self.system.warmup_time:
        #                 self.parts_made += 1
        #         elif self.out_buff.level < self.out_buff.capacity:
        #         # part was finished before failure                        
        #             if self.i < self.system.n-1:
        #                 yield self.out_buff.put(1)
        #                 self.system.state_data.loc[self.env.now, f'b{self.i} level'] = self.out_buff.level
                    
        #             if self.env.now > self.system.warmup_time:
        #                 self.parts_made += 1

        #         self.system.production_data.loc[self.env.now, f'M{self.i} production'] = self.parts_made

        #         self.has_part = False
        # except:
        #     pass
                
        maintenance_start = self.env.now
        if self.system.debug:
            print('  '*self.i+f'M{self.i} starting maintenance at t={self.env.now}')

        # generate TTR based on repair type
        if self.repair_type is not 'planned':
            self.time_to_repair = self.system.repair_params[self.repair_type].rvs()
            if self.system.debug:
                print('  '*self.i+f'M{self.i} TTR={self.time_to_repair} at t={self.env.now}')
            #print(f'M{self.i} TTR={self.time_to_repair}')

        if self.env.now + self.time_to_repair > self.system.warmup_time + self.system.sim_time:
            # repair goes beyond simulation time
            dt = (self.system.warmup_time + self.system.sim_time) - self.env.now 
            #if self.i==0: print(f'Incrementing idle time by {dt} at t={self.env.now}')
            self.total_downtime += dt


        # wait for repair to finish
        for _ in range(self.time_to_repair):
            yield self.env.timeout(1)
            # record queue data
            current_queue = [f'M{machine.i}' for machine in self.system.machines if machine.request_maintenance]
            self.system.queue_data.loc[self.env.now, 'level'] = len(current_queue)
            self.system.queue_data.loc[self.env.now, 'contents'] = str(current_queue)
        
        if self.system.debug:
            print('  '*self.i+f'M{self.i} repaired at t={self.env.now}')

        # repairman is released
        self.maintenance_request = None                                                  

        self.health = 0
        self.last_repair_time = self.env.now
        self.failed = False
        self.down = False
        self.under_repair = False
        self.time_entered_queue = 99999
        #self.request_maintenance = False

        # record restored health
        self.system.machine_data.loc[self.env.now, self.name+' health'] = self.health
        
        maintenance_stop = self.env.now            

        self.system.machine_data.loc[maintenance_start:maintenance_stop-1, f'M{self.i} functional'] = 0
        
        # write repair data
        new_repair = pd.DataFrame({'time':[self.env.now-self.system.warmup_time],
                                    'machine':[self.i],
                                    'type':[self.repair_type],
                                    'activity':['repair'],
                                    'duration':[maintenance_stop-maintenance_start]})
        self.system.maintenance_data = self.system.maintenance_data.append(new_repair)
        
        downtime_stop = self.env.now

        # record idle time due to repair
        if self.env.now > self.system.warmup_time:       
            #if self.i==0: print(f'Incrementing idle time by {downtime_stop-downtime_start} at t={self.env.now}')
            self.total_downtime += (downtime_stop - downtime_start)
        
        # machine was idle before failure                
        self.system.machine_data.loc[self.idle_start:downtime_stop-1, 
                                        self.name+' forced idle'] = 1
        
        # stop degradation if new failures are not allowed
        if not self.allow_new_maintenance:
            self.degradation = np.eye(self.failed_state+1)

        self.system.available_maintenance += 1 # release maintenance resource

    def degrade(self):
        '''
        Discrete state Markovian degradation process. 
        '''
        while True:
            try:
                # stop processing if initialized as failed
                if (self.env.now == 0) and (self.failed):
                    try:
                        self.request_maintenance = True
                        self.process.interrupt()
                    except:
                        pass
                
                # TODO: check placement of this timeout
                yield self.env.timeout(1)

                # sample next health state based on transition matrix
                states = np.arange(0, self.failed_state+1)
                self.health = np.random.choice(states, p=self.degradation[self.health])                            

                # record current machine health
                self.system.machine_data.loc[self.env.now, self.name+' health'] = self.health
                    
                if ((self.health == self.failed_state)
                   and (not self.failed)): # machine fails
                    if self.system.debug and self.i == 0: 
                        print('  '*self.i+f'M{self.i} reached failure at t={self.env.now}')
                    self.failed = True
                    self.repair_type = 'CM'
                    
                    if self.allow_new_maintenance:
                        self.request_maintenance = True
                    
                    if (self.maintenance_policy == 'CM') or (not self.maintenance_policy):
                        self.time_entered_queue = min([self.time_entered_queue, self.env.now])
                    self.write_failure()
                    self.process.interrupt()

                # TODO: validate elif here  
                elif ((self.maintenance_policy == 'CBM') 
                        and (self.health >= self.CBM_threshold) 
                        and (not self.failed)
                        and (self.allow_new_maintenance)
                        and (not self.request_maintenance)):
                    # CBM threshold reached, request repair
                    self.request_maintenance = True
                    self.repair_type = 'CBM'
                    self.time_entered_queue = min([self.time_entered_queue, self.env.now])
                    
                    self.write_failure()

                # elif self.maintenance_policy == 'CBM' and self.health >= self.CBM_threshold:
                #     yield self.env.process(self.maintain())

            except simpy.Interrupt:
                # stop degradation process while machine is under repair
                while self.under_repair:
                    yield self.env.timeout(1)

    def scheduled_failures(self):
        '''
        Check for planned downtime events and request maintenance if flagged for
        preventive repair.
        '''
        while True:
            try:
                # check if a failure is planned
                for failure in self.planned_failures:
                    if failure[1] == self.env.now:
                        self.time_to_repair = failure[2]
                        self.repair_type = 'planned'
                        '''
                        Here we create a maintenance request without interrupting
                        the machine's processing. The process is only interrupted
                        once it seizes a maintenance resource and the job begins.
                        '''                   
                        #THIS METHOD WORKS
                        self.request_maintenance = True
                        self.maintenance_request = self.system.repairman.request(priority=1)
                        
                        yield self.maintenance_request # wait for repairman to become available
                        self.failing.interrupt()
                        self.process.interrupt()

                yield self.env.timeout(1)

            except simpy.Interrupt:
                while self.under_repair:
                    yield self.env.timeout(1)

    def get_priority(self):
        return self.i

    def update_priority(self):
        '''
        Update the maintenance priority for this machine.
        '''
        if (self.maintenance_request) and (not self.under_repair):
            # delete request, update priority 
            self.maintenance_request.cancel()
            priority = self.get_priority()
            self.maintenance_request = self.system.repairman.request(priority=priority)
            yield self.maintenance_request
    
    def write_failure(self):
        '''
        Write new failure occurence to simulation data.
        '''
        if self.system.debug:
            print('  '*self.i+f'Writing {self.repair_type} failure on M{self.i} at t={self.env.now}')
        if self.last_repair_time:
            TTF = self.env.now - self.last_repair_time
        else:
            TTF = 'NA'

        new_failure = pd.DataFrame({'time':[self.env.now-self.system.warmup_time],
                                    'machine':[self.i],
                                    'type':[self.repair_type],
                                    'activity':['failure'],
                                    'duration':[TTF]})

        self.system.maintenance_data = self.system.maintenance_data.append(new_failure, ignore_index=True) 
        